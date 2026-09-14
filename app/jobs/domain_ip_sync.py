import asyncio
from urllib.parse import urlparse

from app.core.logging import logger
from app.core.state import SERVERS_CACHE
from app.services.cloudflare import CloudflareHandler
from app.services.domain_sync import sync_server_domain_ip
from app.storage.repositories import save_servers, save_subs
from app.utils.network import is_ip_literal, resolve_domain_ip


# Cloudflare 后台更新 A 记录后，面板通过这个后台任务轮询发现变化。
# 不是 Cloudflare 主动推送，因此实际生效时间取决于 startup.py 里注册的 interval。
#
# 为了避免 100+ 台机器每轮都打 Cloudflare API，这里先做普通 DNS 预检查：
# 域名解析结果和面板当前保存 IP 一致时，不调用 Cloudflare；只有不一致时才走 API。
DOMAIN_IP_SYNC_CONCURRENCY = 3


def _current_server_ip(srv):
    """取面板当前保存的服务器 IP，用于和主域名 DNS 解析结果做廉价预检查。"""
    ssh_host = str(srv.get('ssh_host') or '').strip()
    if ssh_host and is_ip_literal(ssh_host):
        return ssh_host.strip('[]')

    url_str = str(srv.get('url') or '').strip()
    if url_str:
        try:
            parsed = urlparse(url_str)
            host = parsed.hostname
            if host and is_ip_literal(host):
                return host.strip('[]')
        except Exception:
            pass

    return ''


async def job_sync_domain_ips():
    """定时同步已绑定 Cloudflare 主域名的服务器 IP。

    Lite 版本原本删除了“每小时全量同步”，只在打开单机详情页时同步。
    这里恢复自动同步能力，但不恢复旧版高开销的“按当前 IP 反查域名”。
    每轮采用两段式：

    - 只处理已经设置 `cf_primary_domain` 的服务器；
    - 先用普通 DNS 解析主域名，和面板当前保存 IP 对比；
    - DNS 结果一致时直接跳过，不访问 Cloudflare API；
    - DNS 结果不一致/当前没有可比较 IP 时，才查询 Cloudflare A 记录并回写；
    - 有改动时统一 `save_servers()`，保证 url 主键变化时旧记录会被清理；
    - `sync_server_domain_ip()` 内部若迁移了订阅节点 key，这里同步落库 `save_subs()`。

    注意：如果该记录开启了 Cloudflare 橙云代理，普通 DNS 解析到的是 Cloudflare 边缘 IP，
    不是源站 IP，因此预检查会长期“不一致”，仍需要调用 Cloudflare API 才能知道真实源站 IP。
    """
    targets = [
        s for s in SERVERS_CACHE
        if isinstance(s, dict) and str(s.get('cf_primary_domain') or '').strip()
    ]
    if not targets:
        return

    cf = CloudflareHandler()
    sema = asyncio.Semaphore(DOMAIN_IP_SYNC_CONCURRENCY)
    changed = False

    async def _sync_one(srv):
        async with sema:
            try:
                domain = str(srv.get('cf_primary_domain') or '').strip()
                current_ip = _current_server_ip(srv)
                dns_ip = await resolve_domain_ip(domain, timeout=4.0, use_cache=False)

                if current_ip and dns_ip and current_ip == dns_ip:
                    return False

                if current_ip and dns_ip:
                    logger.info(
                        f"🔎 [域名IP预检查] {srv.get('name', '--')} DNS={dns_ip} 当前={current_ip}，准备查询 Cloudflare A 记录"
                    )
                elif current_ip and not dns_ip:
                    logger.warning(
                        f"⚠️ [域名IP预检查] {srv.get('name', '--')} 主域名 {domain} DNS 解析失败，跳过本轮"
                    )
                    return False
                else:
                    logger.info(
                        f"🔎 [域名IP预检查] {srv.get('name', '--')} 当前无可比较 IP，准备查询 Cloudflare A 记录"
                    )

                return await sync_server_domain_ip(srv, cf=cf)
            except Exception as e:
                logger.warning(f"⚠️ [域名IP定时同步] {srv.get('name', '--')} 跳过: {e}")
                return False

    results = await asyncio.gather(*(_sync_one(s) for s in targets))
    changed = any(results)

    if not changed:
        logger.info(f"✅ [域名IP定时同步] 已检查 {len(targets)} 台，未发现 IP 变化")
        return

    await save_servers()
    await save_subs()

    try:
        from app.ui.components.dashboard import refresh_dashboard_ui
        await refresh_dashboard_ui()
    except Exception as e:
        logger.debug(f"[域名IP定时同步] 刷新仪表盘跳过: {e}")

    try:
        from app.ui.components.sidebar import render_sidebar_content
        render_sidebar_content.refresh()
    except Exception as e:
        logger.debug(f"[域名IP定时同步] 刷新侧边栏跳过: {e}")

    logger.info(f"✅ [域名IP定时同步] 已同步 {sum(1 for x in results if x)} 台服务器的最新 IP")