"""域名 ↔ IP 同步：按需，单台。

原先这是 `app/jobs/domain_ip_sync.py` 里一个每小时全量跑一遍的定时任务：
遍历 SERVERS_CACHE，为每台机器打 1-2 次 Cloudflare API。精简版实测下来这批请求
是启动后那串刷屏 CF 日志的来源，而它真正能起作用的场合极少——`url` 的更新守卫
（`current_host != domain`）保证「url 里填的就是域名」的机器**url 永远不会被更新**，
为它们跑的 CF 往返基本白费。

所以改成按需：只在用户主动打开单机详情页时同步那一台。详情页本来就要查
Cloudflare 记录（`load_cloudflare_records`），顺路做完回写，不增加网络往返。
"""

import asyncio
import socket
from urllib.parse import urlparse, urlunparse

from app.core.logging import logger
from app.core.state import NODES_DATA, PROBE_DATA_CACHE
from app.utils.network import is_ip_literal


def _resolve_ip(domain):
    """故意不走 `resolve_domain_ip` 的 DNS_CACHE：这里要的就是「域名现在指向哪」，
    读缓存等于拿旧答案跟旧答案比，IP 变了也发现不了。"""
    try:
        return socket.gethostbyname(domain)
    except Exception:
        return None


async def sync_server_domain_ip(srv, cf=None):
    """同步单台服务器的域名与 IP。返回 True 表示改动了配置。

    做两件事（与原定时任务逐字对齐，包括那两处更新守卫）：
      1. `cf_primary_domain` 为空、但 Cloudflare 里有指向本机 IP 的 A 记录时，
         取第一条自动绑定为主域名。
      2. 主域名的 A 记录指向了新 IP（机器换 IP 了）时，更新 `url` / `ssh_host`，
         并迁移两个以 url 为键的缓存。

    **调用方负责 save_servers()** —— 这样批量调用时可以只存一次盘。
    """
    if not isinstance(srv, dict):
        return False

    if cf is None:
        from app.services.cloudflare import CloudflareHandler
        cf = CloudflareHandler()

    updated = False

    current_ip = None
    url_str = srv.get('url', '')
    if url_str:
        try:
            parsed = urlparse(url_str)
            current_ip = parsed.hostname
        except Exception:
            pass
    if not current_ip:
        current_ip = srv.get('ssh_host')

    domain = srv.get('cf_primary_domain')
    # 反查只对 IP 有意义：current_ip 落成域名时（url 里填的就是域名）拿它去查
    # list_a_records_by_ip 必然空手而归，白打一次 CF 往返
    if not domain and current_ip and is_ip_literal(current_ip):
        try:
            ok, records = await cf.list_a_records_by_ip(current_ip)
            if ok and records and len(records) > 0:
                domain = records[0].get('name')
                if domain:
                    srv['cf_primary_domain'] = domain
                    updated = True
                    logger.info(f"🔄 [域名自动绑定] {srv.get('name', 'Unknown')} 自动绑定主域名: {domain}")
        except Exception as e:
            logger.warning(f"Failed to auto-bind CF domain for {srv.get('name')}: {e}")

    domain = srv.get('cf_primary_domain')
    if not domain:
        return updated
    domain = domain.strip()
    if not domain:
        return updated

    new_ip = None
    if cf.token:
        ok, ip_or_err = await cf.get_a_record_ip_by_domain(domain)
        if ok and ip_or_err:
            new_ip = ip_or_err

    if not new_ip:
        new_ip = await asyncio.to_thread(_resolve_ip, domain)

    if not new_ip:
        return updated

    url_str = srv.get('url', '')
    if url_str:
        try:
            parsed = urlparse(url_str)
            current_host = parsed.hostname
            if current_host and current_host != new_ip and current_host != domain:
                netloc = new_ip
                if parsed.port:
                    netloc = f"{netloc}:{parsed.port}"
                if parsed.username:
                    auth = parsed.username
                    if parsed.password:
                        auth = f"{auth}:{parsed.password}"
                    netloc = f"{auth}@{netloc}"

                new_url = urlunparse(parsed._replace(netloc=netloc))
                logger.info(f"🔄 [域名IP同步] {srv.get('name', 'Unknown')} URL 更新: {current_host} -> {new_ip}")

                # 这两个缓存都以 url 为键，换了 url 必须一起迁移，
                # 否则探针数据和节点列表会凭空「消失」
                if url_str in PROBE_DATA_CACHE:
                    PROBE_DATA_CACHE[new_url] = PROBE_DATA_CACHE.pop(url_str)
                if url_str in NODES_DATA:
                    NODES_DATA[new_url] = NODES_DATA.pop(url_str)
                # 同步迁移所有订阅中引用的节点 key，否则旧 key 会被判定为"失效"
                from app.services.sub_pipeline import migrate_sub_node_keys
                migrate_sub_node_keys(url_str, new_url)

                srv['url'] = new_url
                updated = True
        except Exception as e:
            logger.warning(f"Failed to parse or update URL for {srv.get('name')}: {e}")

    ssh_host = srv.get('ssh_host')
    # 这里不再拿 `ssh_host != domain` 当守卫：ssh_host 存的就是域名时才最该被换成 IP
    # （SSH 要连的是机器本身，域名过了 CF 橙云会连到边缘节点上去）
    if ssh_host and ssh_host != new_ip:
        logger.info(f"🔄 [域名IP同步] {srv.get('name', 'Unknown')} SSH Host 更新: {ssh_host} -> {new_ip}")
        srv['ssh_host'] = new_ip
        updated = True

    return updated
