import asyncio
import json
import re
import shlex
import socket
import time

from fastapi import Request
from fastapi.responses import Response
from nicegui import run

from app.core.config import (
    AUTO_COUNTRY_MAP,
    AUTO_REGISTER_SECRET,
    PROBE_AGENT_NAME,
    PROBE_AGENT_SCRIPT,
    PROBE_INSTALL_SCRIPT,
    PROBE_LEGACY_AGENT_NAME,
    PROBE_LEGACY_AGENT_SCRIPT,
)
from app.core.logging import logger
from app.core.state import (
    ADMIN_CONFIG,
    NODES_DATA,
    PROBE_DATA_CACHE,
    SERVERS_CACHE,
)
from app.services.ssh import get_ssh_client, _ssh_exec_wrapper
from app.services.xui_fetch import merge_local_node_fields
from app.services.traffic_guard import check_and_handle_traffic_limit
from app.storage.repositories import save_servers
from app.utils.geo import get_flag_for_country


# 延迟导入的跨模块函数：
# - safe_notify / render_sidebar_content 位于 UI 层
# - refresh_dashboard_ui / force_geoip_naming_task 位于业务/页面相关模块
# 为避免当前迁移阶段的循环导入，保持在函数内部按需导入。


# 推送间隔的合法区间与默认值。
# 下限 60 秒：再密就回到「为探针指标服务」的老节奏，而精简版不需要，
# 且软路由上每台每 5 秒一次实测能把面板容器顶到 20-42% CPU。
# 上限 6 小时：用户自己划的范围上界；再长的话「服务器离线」这件事
# 要半天才能发现，判活就彻底失去意义了。
PUSH_INTERVAL_MIN = 60
PUSH_INTERVAL_MAX = 21600
PUSH_INTERVAL_DEFAULT = 1800


def probe_push_interval():
    """agent 应该每隔多少秒推一次。非法值一律回落到默认值。"""
    try:
        interval = int(ADMIN_CONFIG.get('probe_push_interval') or PUSH_INTERVAL_DEFAULT)
    except (TypeError, ValueError):
        return PUSH_INTERVAL_DEFAULT
    return max(PUSH_INTERVAL_MIN, min(PUSH_INTERVAL_MAX, interval))


def probe_offline_after():
    """探针多久没推就算离线。

    = 推送间隔 × 2 + 60 秒：容忍偶发丢一次推送（网络抖动、CF 边缘 503），
    再加一点余量覆盖 agent 侧采样耗时（get_info 自带 1 秒 CPU 采样）和时钟漂移。

    这个式子对**任何**间隔都成立，所以灰度期间新面板配旧 agent（还在 5 秒推）
    只会显得格外新鲜，不会误报离线——这是能先更面板、后更 agent 的前提。
    """
    return probe_push_interval() * 2 + 60


def is_server_monitored(server_conf):
    """这台机器在不在「有探针在报」的监控范围里。

    两种都算：标记了 probe_installed 的，和虽然没标记但确实推过数据的
    （自动注册进来的机器就是后者）。
    """
    if not isinstance(server_conf, dict):
        return False
    return bool(server_conf.get('probe_installed')) or server_conf.get('url') in PROBE_DATA_CACHE


def is_server_offline(server_conf):
    """监控范围内的机器当前是否离线。同步版。

    判据和 get_server_status 完全一致（同一个 probe_offline_after 阈值），
    只是不 async——它本来也没有 IO，纯读内存里的 PROBE_DATA_CACHE。侧边栏的
    render_sidebar_content 是同步的 refreshable，没法 await，所以需要这个。

    **不在监控范围里的机器一律返回 False**：从没装过探针、也从没上报过的机器
    我们对它没有任何观测，把它算成「离线」是编的——而且那会让离线分组在刚导入
    备份、还没装探针的面板上直接塞满所有机器，这个分组也就废了。
    """
    if not is_server_monitored(server_conf):
        return False
    cache = PROBE_DATA_CACHE.get(server_conf.get('url'))
    if not cache:
        # 标了 probe_installed 却一次都没推过：装完没跑起来，或者主控地址 VPS 侧不通
        return True
    return time.time() - cache.get('last_updated', 0) >= probe_offline_after()


def list_offline_servers(servers=None):
    """当前离线的机器，保持传入顺序（默认按 SERVERS_CACHE 的顺序）。"""
    pool = SERVERS_CACHE if servers is None else servers
    return [s for s in pool if isinstance(s, dict) and is_server_offline(s)]


def count_unmonitored_servers(servers=None):
    """既没装探针也从没上报过的机器台数——它们不参与离线判定，UI 上得说明白。"""
    pool = SERVERS_CACHE if servers is None else servers
    return sum(1 for s in pool if isinstance(s, dict) and not is_server_monitored(s))


async def install_probe_on_server(server_conf):
    name = server_conf.get('name', 'Unknown')
    auth_type = server_conf.get('ssh_auth_type', '全局密钥')
    if auth_type == '独立密码' and not server_conf.get('ssh_password'):
        logger.warning(f"⚠️ [Push Agent] {name} 跳过安装：认证方式为独立密码，但未保存 SSH 密码")
        return False
    if auth_type == '独立密钥' and not server_conf.get('ssh_key'):
        logger.warning(f"⚠️ [Push Agent] {name} 跳过安装：认证方式为独立密钥，但未保存 SSH 私钥")
        return False

    my_token = ADMIN_CONFIG.get('probe_token', 'default_token')

    manager_url = ADMIN_CONFIG.get('manager_base_url', 'http://xui-manager:8080')

    real_script = PROBE_INSTALL_SCRIPT \
        .replace("__MANAGER_URL__", manager_url) \
        .replace("__TOKEN__", my_token) \
        .replace("__SERVER_URL__", server_conf['url']) \
        .replace("__PUSH_INTERVAL__", str(probe_push_interval())) \
        .replace("__AGENT_SCRIPT__", PROBE_AGENT_SCRIPT) \
        .replace("__AGENT_NAME__", PROBE_AGENT_NAME) \
        .replace("__LEGACY_AGENT_SCRIPT__", PROBE_LEGACY_AGENT_SCRIPT) \
        .replace("__LEGACY_AGENT_NAME__", PROBE_LEGACY_AGENT_NAME)

    def _sudo_wrap_command(command: str) -> str:
        """后台 SSH 推送安装时使用的非交互式提权包装。

        探针需要写入 /root 和 /etc/systemd/system，并启动 systemd 服务，
        因此非 root SSH 用户必须通过 sudo 提权。这里不使用 `sudo -i`，
        因为 `sudo -i` 会进入交互式登录 shell，后台 exec_command 容易卡住。
        """
        ssh_user = (server_conf.get('ssh_user') or 'root').strip()
        if ssh_user == 'root':
            return command

        if auth_type == '独立密码' and server_conf.get('ssh_password'):
            sudo_password = shlex.quote(server_conf.get('ssh_password', ''))
            return f"printf '%s\\n' {sudo_password} | sudo -S -p '' {command}"

        return f"sudo -n {command}"

    def _extract_install_script_body(script: str) -> str:
        """把配置中的 `bash -c '...'` 模板提取成可通过 stdin 传给 bash 的纯脚本。

        之前直接把整段 `bash -c '...'` 拼到 sudo 后面执行时，模板内部的
        `exec sudo bash "$0" "$@"` 在 `bash -c` 场景下会把 `$0` 解析成 bash
        二进制路径，最终导致远端报：`/usr/bin/bash: cannot execute binary file`。
        后台 SSH 推送本身已经负责 sudo 提权，因此这里移除模板内自提权行，
        并统一用 `sudo bash -s` 执行 stdin 脚本。
        """
        body = script.strip()
        if body.startswith("bash -c '") and body.endswith("'"):
            body = body[len("bash -c '"):-1]
        body = body.lstrip('\n')
        body = re.sub(
            r'(?m)^# 1\. 提升权限\n\[ "\$\(id -u\)" -eq 0 \].*?exit 1; \}\n\n?',
            '',
            body,
            count=1,
        )
        return body.strip() + '\n'

    def _build_install_command(script: str) -> str:
        body = _extract_install_script_body(script)
        eof = 'XFUSION_PROBE_INSTALL_EOF'
        ssh_user = (server_conf.get('ssh_user') or 'root').strip()
        if ssh_user == 'root':
            return f"bash -s <<'{eof}'\n{body}{eof}"

        if auth_type == '独立密码' and server_conf.get('ssh_password'):
            sudo_password = shlex.quote(server_conf.get('ssh_password', ''))
            return f"printf '%s\\n' {sudo_password} | sudo -S -p '' bash -s <<'{eof}'\n{body}{eof}"

        return f"sudo -n bash -s <<'{eof}'\n{body}{eof}"

    install_command = _build_install_command(real_script)

    try:
        success, output = await _ssh_exec_wrapper(server_conf, install_command, timeout=120)
        if success:
            verify_cmd = _sudo_wrap_command(
                f"test -f {PROBE_AGENT_SCRIPT}"
                f" && test -f /etc/systemd/system/{PROBE_AGENT_NAME}.service"
                f" && systemctl is-active --quiet {PROBE_AGENT_NAME}"
            )
            verify_success, verify_output = await _ssh_exec_wrapper(server_conf, verify_cmd, timeout=30)
            if verify_success:
                success, msg = True, "Agent 安装成功并启动"
                if 'XFUSION_LEGACY_AGENT_REMOVED' in (output or ''):
                    logger.info(
                        f"🧹 [Push Agent] {name} 已清理本面板早期版本留下的 {PROBE_LEGACY_AGENT_NAME}"
                    )
            else:
                success, msg = False, f"安装后校验失败: {verify_output}"
        else:
            success, msg = False, f"安装脚本错误: {output}"
    except Exception as e:
        success, msg = False, f"异常: {str(e)}"
    if success:
        server_conf['probe_installed'] = True
        await save_servers()
        logger.info(f"✅ [Push Agent] {name} 部署成功")
    else:
        server_conf['probe_installed'] = False
        await save_servers()
        logger.warning(f"⚠️ [Push Agent] {name} 部署失败: {msg}")
    return success


async def get_server_status(server_conf):
    raw_url = server_conf['url']

    if server_conf.get('probe_installed', False) or raw_url in PROBE_DATA_CACHE:
        cache = PROBE_DATA_CACHE.get(raw_url)
        if cache:
            if time.time() - cache.get('last_updated', 0) < probe_offline_after():
                return cache
            else:
                return {'status': 'offline', 'msg': '探针离线 (超时)'}

    return {'status': 'offline', 'msg': '未安装探针'}


async def probe_push_data(request: Request):
    try:
        data = await request.json()
        token = data.get('token')
        server_url = data.get('server_url')

        correct_token = ADMIN_CONFIG.get('probe_token')
        if not token or token != correct_token:
            return Response("Invalid Token", 403)

        target_server = next((s for s in SERVERS_CACHE if s['url'] == server_url), None)
        if not target_server:
            try:
                push_ip = server_url.split('://')[-1].split(':')[0]
                for s in SERVERS_CACHE:
                    cache_ip = s['url'].split('://')[-1].split(':')[0]
                    if cache_ip == push_ip:
                        target_server = s
                        break
            except:
                pass

        if not target_server:
            try:
                client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(',')[0].strip()
                for s in SERVERS_CACHE:
                    cache_ip = s['url'].split('://')[-1].split(':')[0]
                    if cache_ip == client_ip:
                        target_server = s
                        break
            except:
                pass

        if target_server:
            if not target_server.get('probe_installed'):
                target_server['probe_installed'] = True

            data['server_url'] = target_server['url']
            data['status'] = 'online'
            data['last_updated'] = time.time()
            PROBE_DATA_CACHE[target_server['url']] = data

            if 'xui_data' in data and isinstance(data['xui_data'], list):
                raw_nodes = data['xui_data']
                parsed_nodes = []
                for n in raw_nodes:
                    try:
                        if isinstance(n.get('settings'), str):
                            n['settings'] = json.loads(n['settings'])
                        if isinstance(n.get('streamSettings'), str):
                            n['streamSettings'] = json.loads(n['streamSettings'])
                        parsed_nodes.append(n)
                    except:
                        parsed_nodes.append(n)

                parsed_nodes = merge_local_node_fields(target_server['url'], parsed_nodes)
                NODES_DATA[target_server['url']] = parsed_nodes
                target_server['_status'] = 'online'

                if parsed_nodes:
                    first_remark = parsed_nodes[0].get('remark', '').strip()
                    current_name = target_server.get('name', '').strip()

                    if first_remark and (first_remark not in current_name):
                        has_own_flag = False
                        for v in AUTO_COUNTRY_MAP.values():
                            known_flag = v.split(' ')[0]
                            if known_flag in first_remark:
                                has_own_flag = True
                                break

                        if has_own_flag:
                            new_name_candidate = first_remark
                        else:
                            flag = "🏳️"
                            if ' ' in current_name:
                                parts = current_name.split(' ', 1)
                                if len(parts[0]) < 10:
                                    flag = parts[0]
                            else:
                                try:
                                    from app.core.state import IP_GEO_CACHE

                                    ip_key = target_server['url'].split('://')[-1].split(':')[0]
                                    geo_info = IP_GEO_CACHE.get(ip_key)
                                    if geo_info:
                                        flag = get_flag_for_country(geo_info[2]).split(' ')[0]
                                except:
                                    pass

                            new_name_candidate = f"{flag} {first_remark}"

                        if target_server['name'] != new_name_candidate:
                            target_server['name'] = new_name_candidate
                            asyncio.create_task(save_servers())
                            logger.info(f"🏷️ [探针同步] 根据节点备注自动改名: {new_name_candidate}")

            asyncio.create_task(check_and_handle_traffic_limit(target_server, data))

        # 响应体里带上期望的推送间隔，agent 会用它当下次 sleep 的秒数。
        # 这样改间隔只需要在面板里改个数字，不用再 SSH 重装所有 VPS。
        # 老版本 agent 只判断请求是否成功、不读响应体，多出来的数字会被忽略。
        return Response(f"OK {probe_push_interval()}", 200)
    except Exception:
        return Response("Error", 500)


async def probe_register(request: Request):
    try:
        data = await request.json()

        submitted_token = data.get('token')
        correct_token = ADMIN_CONFIG.get('probe_token')

        if not submitted_token or submitted_token != correct_token:
            return Response(json.dumps({"success": False, "msg": "Token 错误"}), status_code=403)

        client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(',')[0].strip()

        target_server = None

        for s in SERVERS_CACHE:
            if client_ip in s['url']:
                target_server = s
                break

        if not target_server:
            logger.info(f"🔍 [探针注册] IP {client_ip} 未直接匹配，尝试解析现有域名...")
            for s in SERVERS_CACHE:
                try:
                    cached_host = s['url'].split('://')[-1].split(':')[0]

                    if re.match(r"^\d+\.\d+\.\d+\.\d+$", cached_host):
                        continue

                    resolved_ip = await run.io_bound(socket.gethostbyname, cached_host)

                    if resolved_ip == client_ip:
                        target_server = s
                        logger.info(f"✅ [探针注册] 域名 {cached_host} 解析为 {client_ip}，匹配成功！")
                        break
                except:
                    pass

        if target_server:
            if not target_server.get('probe_installed'):
                target_server['probe_installed'] = True
                await save_servers()

                from app.ui.components.dashboard import refresh_dashboard_ui

                await refresh_dashboard_ui()

            return Response(json.dumps({"success": True, "msg": "已合并现有服务器"}), status_code=200)

        else:
            new_server = {
                'name': f"🏳️ {client_ip}",
                'group': '自动注册',
                'url': f"http://{client_ip}:54321",
                'ssh_auth_type': '全局密钥',
                'probe_installed': True,
                '_status': 'online',
            }
            SERVERS_CACHE.append(new_server)
            await save_servers()

            from app.services.server_ops import force_geoip_naming_task
            from app.ui.components.dashboard import refresh_dashboard_ui
            from app.ui.components.sidebar import render_sidebar_content

            asyncio.create_task(force_geoip_naming_task(new_server))

            await refresh_dashboard_ui()
            try:
                render_sidebar_content.refresh()
            except:
                pass

            logger.info(f"✨ [主动注册] 新服务器上线: {client_ip}")
            return Response(json.dumps({"success": True, "msg": "注册成功"}), status_code=200)

    except Exception as e:
        logger.error(f"❌ 注册接口异常: {e}")
        return Response(json.dumps({"success": False, "msg": str(e)}), status_code=500)


async def smart_detect_ssh_user_task(server_conf):
    """
    后台任务：尝试使用不同的用户名 (ubuntu -> root) 连接 SSH。
    连接成功后：
    1. 更新配置并保存。
    2. 自动触发探针安装。
    """
    candidates = ['root', 'ubuntu']

    ip = server_conf['url'].split('://')[-1].split(':')[0]
    original_user = server_conf.get('ssh_user', '')

    logger.info(f"🕵️‍♂️ [智能探测] 开始探测 {server_conf['name']} ({ip}) 的 SSH 用户名...")

    found_user = None

    for user in candidates:
        server_conf['ssh_user'] = user

        client, msg = await get_ssh_client(server_conf)

        if client:
            client.close()
            found_user = user
            logger.info(f"✅ [智能探测] 成功匹配用户名: {user}")
            break
        else:
            logger.warning(f"⚠️ [智能探测] 用户名 '{user}' 连接失败，尝试下一个...")

    if found_user:
        server_conf['ssh_user'] = found_user
        server_conf['_ssh_verified'] = True
        await save_servers()

        if ADMIN_CONFIG.get('probe_enabled', False):
            logger.info(f"🚀 [自动部署] SSH 验证通过，开始安装探针...")
            await asyncio.sleep(2)
            await install_probe_on_server(server_conf)

    else:
        logger.error(f"❌ [智能探测] {server_conf['name']} 所有用户名均尝试失败 (请检查安全组或密钥)")
        if original_user:
            server_conf['ssh_user'] = original_user
        await save_servers()


async def auto_register_node(request: Request):
    try:
        data = await request.json()

        secret = data.get('secret')
        if secret != AUTO_REGISTER_SECRET:
            logger.warning(f"⚠️ [自动注册] 密钥错误: {'*' * len(str(secret or ''))}")
            return Response(json.dumps({"success": False, "msg": "密钥错误"}), status_code=403, media_type="application/json")

        ip = data.get('ip')
        port = data.get('port')
        alias = data.get('alias', f'Auto-{ip}')

        ssh_port = data.get('ssh_port', 22)

        # 面板凭据已不再使用；老调用方即使继续发送 username/password 也只会被忽略
        if not all([ip, port]):
            return Response(json.dumps({"success": False, "msg": "参数不完整"}), status_code=400, media_type="application/json")

        target_url = f"http://{ip}:{port}"

        new_server_config = {
            'name': alias,
            'group': '默认分组',
            'url': target_url,
            'ssh_port': ssh_port,
            'ssh_auth_type': '全局密钥',
            'ssh_user': 'detecting...',
            'probe_installed': False,
        }

        existing_index = -1
        for idx, srv in enumerate(SERVERS_CACHE):
            cache_url = srv['url'].replace('http://', '').replace('https://', '')
            new_url_clean = target_url.replace('http://', '').replace('https://', '')
            if cache_url == new_url_clean:
                existing_index = idx
                break

        action_msg = ""
        target_server_ref = None

        if existing_index != -1:
            SERVERS_CACHE[existing_index].update(new_server_config)
            target_server_ref = SERVERS_CACHE[existing_index]
            action_msg = f"🔄 更新节点: {alias}"
        else:
            SERVERS_CACHE.append(new_server_config)
            target_server_ref = new_server_config
            action_msg = f"✅ 新增节点: {alias}"

        await save_servers()

        from app.services.server_ops import force_geoip_naming_task
        from app.ui.components.sidebar import render_sidebar_content

        asyncio.create_task(force_geoip_naming_task(target_server_ref))
        asyncio.create_task(smart_detect_ssh_user_task(target_server_ref))

        try:
            render_sidebar_content.refresh()
        except:
            pass

        logger.info(f"[自动注册] {action_msg} ({ip}) - 已加入 SSH 探测与命名队列")
        return Response(json.dumps({"success": True, "msg": "注册成功，后台正在探测连接..."}), status_code=200, media_type="application/json")

    except Exception as e:
        logger.error(f"❌ [自动注册] 处理异常: {e}")
        return Response(json.dumps({"success": False, "msg": str(e)}), status_code=500, media_type="application/json")
