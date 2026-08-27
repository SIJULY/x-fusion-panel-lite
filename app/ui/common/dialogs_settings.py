import asyncio
import csv
import io
import ipaddress
import socket
import time
from datetime import datetime
from urllib.parse import urlparse

from nicegui import app, run, ui


def _settings_theme():
    is_dark = bool(app.storage.user.get('is_dark', True))
    return {
        'is_dark': is_dark,
        'card': 'w-[500px] max-w-[92vw] p-0 gap-0 flex flex-col overflow-hidden rounded-sm bg-[#070b14] border border-[#1e3a5f]/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-[500px] max-w-[92vw] p-0 gap-0 flex flex-col overflow-hidden rounded-sm bg-white border border-slate-300/90 shadow-[0_10px_28px_rgba(148,163,184,0.18)]',
        'wide_card': 'w-full max-w-2xl p-0 gap-0 flex flex-col overflow-hidden rounded-sm bg-[#070b14] border border-[#1e3a5f]/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-full max-w-2xl p-0 gap-0 flex flex-col overflow-hidden rounded-sm bg-white border border-slate-300/90 shadow-[0_10px_28px_rgba(148,163,184,0.18)]',
        'header': 'w-full p-5 gap-3 bg-gradient-to-r from-[#0a1526] to-[#050a14] border-b border-[#1e3a5f]/60' if is_dark else 'w-full p-5 gap-3 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff] border-b border-slate-300/90',
        'header_row': 'justify-between items-center w-full px-5 py-4 border-b border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14]' if is_dark else 'justify-between items-center w-full px-5 py-4 border-b border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff]',
        'icon_box': 'w-9 h-9 rounded-sm flex items-center justify-center bg-[#050b14] border border-[#1e3a5f] shadow-[0_0_8px_rgba(0,0,0,0.7)] relative overflow-hidden' if is_dark else 'w-9 h-9 rounded-sm flex items-center justify-center bg-sky-50 border border-slate-300 shadow-[0_4px_12px_rgba(148,163,184,0.14)] relative overflow-hidden',
        'title': 'text-lg font-black text-slate-100 tracking-wide' if is_dark else 'text-lg font-black text-slate-800 tracking-wide',
        'sub': 'text-xs text-slate-400' if is_dark else 'text-xs text-slate-500',
        'body': 'w-full p-5 gap-4 bg-[#030712]' if is_dark else 'w-full p-5 gap-4 bg-[#f8fbff]',
        'scroll': 'w-full h-[60vh] pr-4 bg-[#030712]' if is_dark else 'w-full h-[60vh] pr-4 bg-[#f8fbff]',
        'input_props': 'outlined dense dark color=cyan standout bg-color="[#050b14]"' if is_dark else 'outlined dense color=blue',
        'password_props': 'outlined dense type=password dark color=cyan standout bg-color="[#050b14]"' if is_dark else 'outlined dense type=password color=blue',
        'footer': 'w-full justify-end mt-4 p-4 border-t border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14]' if is_dark else 'w-full justify-end mt-4 p-4 border-t border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff]',
        'footer_full': 'w-full p-4 border-t border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14]' if is_dark else 'w-full p-4 border-t border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff]',
        'cancel': 'text-slate-300 border-slate-600 hover:bg-slate-800/40 text-xs font-bold tracking-wide rounded-sm' if is_dark else 'text-slate-600 border-slate-300 hover:bg-slate-100 text-xs font-bold tracking-wide rounded-sm',
        'save': 'bg-cyan-950/45 text-cyan-300 border border-cyan-500/45 hover:bg-cyan-900/55 font-black rounded-sm px-5' if is_dark else 'bg-sky-100 text-sky-700 border border-sky-300 hover:bg-sky-200 font-black rounded-sm px-5',
        'save_full': 'w-full bg-cyan-950/45 text-cyan-300 border border-cyan-500/45 hover:bg-cyan-900/55 shadow-[0_0_12px_rgba(34,211,238,0.22)] h-12 font-black rounded-sm' if is_dark else 'w-full bg-sky-100 text-sky-700 border border-sky-300 hover:bg-sky-200 shadow-[0_6px_16px_rgba(56,189,248,0.16)] h-12 font-black rounded-sm',
    }

from app.core.logging import logger
from app.core.state import ADMIN_CONFIG, PROBE_DATA_CACHE, SERVERS_CACHE
from app.services.cloudflare import CloudflareHandler
from app.services.probe import install_probe_on_server
from app.storage.repositories import save_admin_config
from app.ui.common.notifications import safe_notify


def _extract_server_ip(server_conf):
    url = str(server_conf.get('url', '') or '').strip()
    if url:
        try:
            parsed = urlparse(url if '://' in url else f'http://{url}')
            host = parsed.hostname
            if host:
                return socket.gethostbyname(host)
        except Exception:
            pass

    ssh_host = str(server_conf.get('ssh_host', '') or '').strip()
    if ssh_host:
        try:
            return socket.gethostbyname(ssh_host)
        except Exception:
            return ssh_host

    return ''


def _manager_url_warning(url):
    """主控地址明显不可能被 VPS 访问到时返回一句提示，否则返回 None。

    探针是 VPS 主动往面板 POST（方向是 VPS → 面板），所以这个地址必须是
    **VPS 那一侧能访问到的**。填容器内部名或内网地址是最常见的翻车点，尤其
    是把面板部署在软路由 / NAS 上的时候——面板自己访问得好好的，VPS 却永远
    连不上，表现是装完探针一直「探针离线 (超时)」。
    这里只提示不拦截：面板和机器都在同一个内网时，内网地址是合法配置。
    """
    raw = str(url or '').strip()
    if not raw:
        return '主控端外部地址还没填，探针不知道该往哪推'

    host = urlparse(raw if '://' in raw else f'http://{raw}').hostname or ''
    if not host:
        return f'主控端外部地址 {raw} 解析不出主机名'

    if host in ('localhost', '0.0.0.0', 'xui-manager'):
        return f'主控地址是容器 / 本机内部地址（{host}），VPS 连不上'

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 是域名，无法在这里判断可达性，交给实际安装结果说话
        return None

    if ip.is_loopback:
        return f'主控地址是本机回环地址（{host}），VPS 连不上'
    if ip.is_private:
        return f'主控地址是内网地址（{host}）。只有当这些机器和面板处在同一内网时才能通，否则要换成公网 IP / 域名 / 隧道地址'
    return None


def _probe_push_age(server_conf):
    """该机器最近一次探针推送距今多少秒；从未推送过返回 None。"""
    cache = PROBE_DATA_CACHE.get(server_conf.get('url'))
    if not cache:
        return None
    ts = cache.get('last_updated', 0)
    if not ts:
        return None
    return max(0.0, time.time() - ts)


def _probe_skip_reason(server_conf):
    """安装前的凭据自检：返回 None 表示可以装，否则返回跳过原因。

    install_probe_on_server 内部也会做同样这两项检查并直接返回 False，但那样
    用户在批量结果里只能看到一个笼统的「失败」。这里提前分类，把「根本没凭据、
    连都不用连」和「连上了但装失败」区分开，方便对症处理。
    """
    if not isinstance(server_conf, dict) or not server_conf.get('url'):
        return '记录缺少 url'
    auth = server_conf.get('ssh_auth_type', '全局密钥')
    if auth == '独立密码' and not server_conf.get('ssh_password'):
        return '未保存 SSH 密码'
    if auth == '独立密钥' and not server_conf.get('ssh_key'):
        return '未保存 SSH 私钥'
    return None


def open_batch_probe_dialog():
    """一键给所有服务器安装 / 更新探针。

    主要场景是「导入了完整版或另一台面板的备份」：机器和节点配置都恢复了，
    但那些 VPS 上的 agent 仍然只上报给原来的面板，本面板会一直显示探针离线，
    而原来只能一台一台进详情页重装。改主控端外部地址之后也需要把所有 agent
    重新推一遍，否则它们还在往旧地址推。
    """
    theme = _settings_theme()
    is_dark = theme['is_dark']

    with ui.dialog() as d, ui.card().classes(theme['wide_card']):
        with ui.row().classes(theme['header_row']):
            with ui.row().classes('items-center gap-3'):
                with ui.element('div').classes(theme['icon_box'] + (' text-cyan-400' if is_dark else ' text-sky-600')):
                    ui.element('div').classes('absolute inset-0 bg-cyan-400/10' if is_dark else 'absolute inset-0 bg-sky-400/10')
                    ui.icon('sync').classes('text-[18px] drop-shadow-[0_0_5px_currentColor]')
                ui.label('一键更新所有探针').classes(theme['title'])
            close_btn = ui.button(icon='close', on_click=d.close).props('flat round dense color=grey').classes(
                'text-slate-400 hover:text-cyan-300 hover:bg-cyan-950/30' if is_dark else 'text-slate-500 hover:text-sky-700 hover:bg-sky-100')

        with ui.column().classes(theme['body']):
            manager_url = str(ADMIN_CONFIG.get('manager_base_url', '') or '').strip()
            ui.label(f'所有 agent 都会被重装成上报到：{manager_url or "（未设置）"}').classes(
                'text-xs ' + ('text-slate-300' if is_dark else 'text-slate-600'))
            ui.label(
                '只覆盖本面板自己的 agent（x-fusion-agent-lite），不碰完整版的探针，'
                '也不碰 xray / x-ui / hysteria / snell，节点不会断。'
            ).classes('text-[11px] ' + ('text-slate-500' if is_dark else 'text-slate-500'))

            url_warn = _manager_url_warning(manager_url)
            if url_warn:
                with ui.row().classes(
                    'w-full items-start gap-2 p-3 rounded-sm '
                    + ('bg-amber-950/30 border border-amber-500/40' if is_dark else 'bg-amber-50 border border-amber-300')
                ):
                    ui.icon('warning').classes('text-amber-400 text-[16px] mt-[2px]')
                    ui.label(url_warn).classes(
                        'text-[11px] leading-relaxed flex-1 ' + ('text-amber-200' if is_dark else 'text-amber-800'))

            skip_chk = ui.checkbox('跳过当前探针已在线的机器', value=False).props(
                'dense' + (' dark color=cyan' if is_dark else ' color=blue')).classes('text-xs')
            ui.label(
                '刚导入备份时全部机器都是离线的，不用勾。只有在「已经有一批机器正常上报、'
                '只想补装剩下的」时才勾上。'
            ).classes('text-[11px] ' + ('text-slate-500' if is_dark else 'text-slate-500'))

            count_lbl = ui.label('').classes(
                'text-xs font-bold ' + ('text-cyan-300' if is_dark else 'text-sky-700'))
            progress_lbl = ui.label('').classes('text-xs ' + ('text-slate-400' if is_dark else 'text-slate-500'))

            progress_box = ui.column().classes('w-full gap-0 max-h-[38vh] overflow-y-auto')

        def _build_plan():
            """把 SERVERS_CACHE 变成 [(server, 跳过原因 or None)]，顺序与侧边栏一致。"""
            plan = []
            for s in SERVERS_CACHE:
                reason = _probe_skip_reason(s)
                if reason is None and skip_chk.value:
                    age = _probe_push_age(s)
                    if age is not None and age < 60:
                        reason = f'已在线（{int(age)} 秒前推送），跳过'
                plan.append((s, reason))
            return plan

        def _refresh_count():
            plan = _build_plan()
            todo = sum(1 for _, reason in plan if reason is None)
            count_lbl.text = f'共 {len(plan)} 台，将安装 / 更新 {todo} 台，跳过 {len(plan) - todo} 台'

        skip_chk.on_value_change(lambda _: _refresh_count())
        _refresh_count()

        async def start():
            plan = _build_plan()
            todo = [(i, s) for i, (s, reason) in enumerate(plan) if reason is None]
            if not todo:
                safe_notify('没有可安装的机器（都被跳过了）', 'warning')
                return

            start_btn.props('loading')
            start_btn.set_enabled(False)
            cancel_btn.set_enabled(False)
            close_btn.set_enabled(False)
            skip_chk.set_enabled(False)
            progress_box.clear()

            rows = {}
            with progress_box:
                for i, (s, reason) in enumerate(plan):
                    with ui.row().classes('w-full items-center gap-2 py-[3px]'):
                        icon_lbl = ui.label('⏳' if reason is None else '⏭').classes('text-[13px] w-5 shrink-0')
                        ui.label(str(s.get('name') or s.get('url') or f'#{i + 1}')).classes(
                            'text-xs flex-1 truncate ' + ('text-slate-200' if is_dark else 'text-slate-700'))
                        detail_lbl = ui.label(reason or '等待中').classes(
                            'text-[11px] shrink-0 ' + ('text-slate-500' if is_dark else 'text-slate-500'))
                    rows[i] = (icon_lbl, detail_lbl)

            total = len(todo)
            done = ok = fail = 0
            # 安装脚本要 apt/yum 装依赖，单台最长 120 秒，串行会等到天荒地老；
            # 但也不能全部并发——每台都是一条独立 SSH 连接 + 一次包管理器操作，
            # 并发太高容易撞上超时。5 是个稳妥的折中。
            sema = asyncio.Semaphore(5)

            def _tick():
                progress_lbl.text = f'进度 {done}/{total}　成功 {ok}　失败 {fail}'

            _tick()

            async def worker(i, s):
                nonlocal done, ok, fail
                icon_lbl, detail_lbl = rows[i]
                async with sema:
                    icon_lbl.text = '🔄'
                    detail_lbl.text = '安装中...'
                    try:
                        good = await install_probe_on_server(s)
                        err = ''
                    except Exception as e:
                        good, err = False, str(e)
                    if good:
                        ok += 1
                        icon_lbl.text = '✅'
                        detail_lbl.text = '已更新'
                    else:
                        fail += 1
                        icon_lbl.text = '❌'
                        # 具体原因 install_probe_on_server 已经写进日志了，
                        # 这里只给一句能对症的提示，详情让用户看容器日志。
                        detail_lbl.text = f'失败：{err}' if err else '失败（连接或安装出错，详见日志）'
                    done += 1
                    _tick()

            try:
                await asyncio.gather(*(worker(i, s) for i, s in todo))
            finally:
                start_btn.props(remove='loading')
                cancel_btn.set_enabled(True)
                close_btn.set_enabled(True)
                skip_chk.set_enabled(True)
                start_btn.set_enabled(True)

            logger.info(f"🔄 [批量探针] 完成：成功 {ok} / 失败 {fail} / 共 {total}")
            if fail:
                safe_notify(
                    f'批量更新完成：成功 {ok} 台，失败 {fail} 台。失败的多半是 SSH 连不上或凭据不对，'
                    f'可在列表里逐台确认。',
                    'warning', timeout=12000,
                )
            else:
                safe_notify(
                    f'✅ {ok} 台探针已更新。agent 每 5 秒推一次，状态大约 10 秒内变绿。',
                    'positive', timeout=8000,
                )

            # 探针状态、节点数、仪表盘都跟着变，刷一遍免得用户以为没生效
            try:
                from app.ui.components.sidebar import render_sidebar_content

                render_sidebar_content.refresh()
            except Exception:
                pass
            try:
                from app.ui.components.dashboard import refresh_dashboard_ui

                refresh_dashboard_ui.refresh()
            except Exception:
                pass

        with ui.row().classes(theme['footer']):
            cancel_btn = ui.button('取消', on_click=d.close).props('outline color=grey').classes(theme['cancel'])
            start_btn = ui.button('开始更新', icon='sync', on_click=start).props('flat').classes(theme['save'])
    d.open()


def open_cloudflare_settings_dialog():
    theme = _settings_theme()
    with ui.dialog() as d, ui.card().classes(theme['card']):
        with ui.column().classes(theme['header']):
            with ui.row().classes('items-center gap-3 text-cyan-400' if theme['is_dark'] else 'items-center gap-3 text-sky-600'):
                with ui.element('div').classes(theme['icon_box']):
                    ui.element('div').classes('absolute inset-0 bg-cyan-400/10' if theme['is_dark'] else 'absolute inset-0 bg-sky-400/10')
                    ui.icon('cloud').classes('text-[18px] drop-shadow-[0_0_5px_currentColor]')
                ui.label('Cloudflare API 配置').classes(theme['title'])
            ui.label('用于自动解析域名、开启 CDN 和设置 SSL (Flexible)。').classes(theme['sub'])

        with ui.column().classes(theme['body']):
            cf_token = ui.input('API Token', value=ADMIN_CONFIG.get('cf_api_token', '')).props(theme['password_props']).classes('w-full')
            ui.label('权限要求: Zone.DNS (Edit), Zone.Settings (Edit)').classes('text-[10px] text-slate-500 ml-1')

            with ui.row().classes('w-full items-end gap-2'):
                cf_domain_root = ui.select([], label='根域名').props(theme['input_props']).classes('flex-1')
                ui.button('刷新域名', icon='refresh', on_click=lambda: refresh_zones(True)).props('flat').classes(theme['save'])

            async def refresh_zones(show_notify=False):
                token_val = cf_token.value.strip()
                saved_root = ADMIN_CONFIG.get('cf_root_domain', '').strip()
                current_value = str(cf_domain_root.value or '').strip() or saved_root
                if not token_val:
                    cf_domain_root.options = [saved_root] if saved_root else []
                    cf_domain_root.value = saved_root or None
                    if show_notify:
                        safe_notify('请先输入 Cloudflare API Token', 'warning')
                    return

                handler = CloudflareHandler()
                handler.token = token_val
                ok, result = await handler.list_zones()
                if ok:
                    zones = [item.get('name', '') for item in (result or []) if item.get('name')]
                    cf_domain_root.options = zones
                    if current_value in zones:
                        cf_domain_root.value = current_value
                    elif saved_root in zones:
                        cf_domain_root.value = saved_root
                    elif zones:
                        cf_domain_root.value = zones[0]
                    else:
                        cf_domain_root.value = None
                    if show_notify:
                        safe_notify(f'已获取 {len(zones)} 个 Cloudflare 域名', 'positive')
                else:
                    fallback = [saved_root] if saved_root else []
                    cf_domain_root.options = fallback
                    cf_domain_root.value = saved_root or None
                    if show_notify:
                        safe_notify(str(result), 'warning')

            async def export_cloudflare_data():
                token_val = cf_token.value.strip()
                if not token_val:
                    safe_notify('请先输入 Cloudflare API Token', 'warning')
                    return

                export_btn.props('loading')
                try:
                    handler = CloudflareHandler()
                    handler.token = token_val
                    handler.root_domain = str(cf_domain_root.value or ADMIN_CONFIG.get('cf_root_domain', '')).strip()
                    ok, result = await handler.list_all_a_records()
                    if not ok:
                        safe_notify(str(result), 'warning')
                        return

                    domains_by_ip = {}
                    for rec in result or []:
                        ip = str(rec.get('content', '') or '').strip()
                        domain = str(rec.get('name', '') or '').strip()
                        if ip and domain:
                            domains_by_ip.setdefault(ip, []).append(domain)

                    def build_csv():
                        output = io.StringIO()
                        writer = csv.writer(output)
                        writer.writerow(['服务器名称', '服务器IP', '服务器IP对应的域名解析'])

                        for server in SERVERS_CACHE:
                            if not isinstance(server, dict):
                                continue
                            server_name = str(server.get('name', '') or '').strip()
                            server_ip = _extract_server_ip(server)
                            domains = domains_by_ip.get(server_ip, [])
                            writer.writerow([server_name, server_ip, '\n'.join(domains)])

                        return output.getvalue()

                    csv_content = await run.io_bound(build_csv)

                    filename = f"cloudflare_server_dns_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                    ui.download(csv_content.encode('utf-8-sig'), filename)
                    safe_notify(f'已导出 {len(SERVERS_CACHE)} 台服务器数据', 'positive')
                except Exception as e:
                    safe_notify(f'导出失败: {e}', 'negative')
                finally:
                    export_btn.props(remove='loading')


        async def save_cf():
            ADMIN_CONFIG['cf_api_token'] = cf_token.value.strip()
            ADMIN_CONFIG['cf_root_domain'] = str(cf_domain_root.value or '').strip()
            await save_admin_config()
            safe_notify('✅ Cloudflare 配置已保存', 'positive')
            d.close()

        with ui.row().classes(theme['footer']):
            export_btn = ui.button('导出数据', icon='download', on_click=export_cloudflare_data).props('outline color=cyan').classes(theme['cancel'])
            ui.button('取消', on_click=d.close).props('outline color=grey').classes(theme['cancel'])
            ui.button('保存配置', on_click=save_cf).props('flat').classes(theme['save'])

        ui.timer(0.1, lambda: refresh_zones(False), once=True)
    d.open()


def open_probe_settings_dialog():
    theme = _settings_theme()
    with ui.dialog() as d, ui.card().classes(theme['wide_card']):
        with ui.row().classes(theme['header_row']):
            with ui.row().classes('items-center gap-3'):
                with ui.element('div').classes(theme['icon_box'] + (' text-cyan-400' if theme['is_dark'] else ' text-sky-600')):
                    ui.element('div').classes('absolute inset-0 bg-cyan-400/10' if theme['is_dark'] else 'absolute inset-0 bg-sky-400/10')
                    ui.icon('tune').classes('text-[18px] drop-shadow-[0_0_5px_currentColor]')
                ui.label('连接与通知设置').classes(theme['title'])
            ui.button(icon='close', on_click=d.close).props('flat round dense color=grey').classes('text-slate-400 hover:text-cyan-300 hover:bg-cyan-950/30' if theme['is_dark'] else 'text-slate-500 hover:text-sky-700 hover:bg-sky-100')

        with ui.scroll_area().classes(theme['scroll']):
            with ui.column().classes('w-full gap-6'):
                with ui.column().classes('w-full bg-cyan-950/15 p-4 rounded-sm border border-cyan-500/25' if theme['is_dark'] else 'w-full bg-sky-50 p-4 rounded-sm border border-sky-200'):
                    ui.label('📡 主控端外部地址 (Agent连接地址)').classes('text-sm font-black text-cyan-300' if theme['is_dark'] else 'text-sm font-black text-sky-700')
                    ui.label('Agent 将向此地址推送数据。请填写 http://公网IP:端口 或 https://域名').classes('text-xs text-cyan-500/80 mb-2' if theme['is_dark'] else 'text-xs text-sky-700/80 mb-2')
                    default_url = ADMIN_CONFIG.get('manager_base_url', 'http://xui-manager:8080')
                    url_input = ui.input(value=default_url, placeholder='http://1.2.3.4:8080').classes('w-full').props(theme['input_props'])

                with ui.column().classes('w-full'):
                    ui.label('🤖 Telegram 通知').classes('text-sm font-black text-slate-200' if theme['is_dark'] else 'text-sm font-black text-slate-800')
                    ui.label(
                        '服务器掉线 / 恢复报警。巡检每 120 秒一轮，连续 3 轮失败才报警（约 6 分钟'
                        '才发出，避免网络抖动误报）。只巡检已装探针的机器。'
                    ).classes('text-xs text-slate-500 mb-2')

                    with ui.grid().classes('w-full grid-cols-1 sm:grid-cols-2 gap-3'):
                        tg_token = ui.input('Bot Token', value=ADMIN_CONFIG.get('tg_bot_token', '')).props(theme['input_props'])
                        tg_id = ui.input('Chat ID', value=ADMIN_CONFIG.get('tg_chat_id', '')).props(theme['input_props'])

                with ui.column().classes('w-full'):
                    ui.label('🔄 批量更新探针').classes('text-sm font-black text-slate-200' if theme['is_dark'] else 'text-sm font-black text-slate-800')
                    ui.label(
                        '给所有服务器一次性安装 / 更新 agent。导入完整版（或另一台面板）的备份后'
                        '用这个把上报切到本面板；改完上面的主控端外部地址也要跑一次，否则 agent '
                        '还在往旧地址推。'
                    ).classes('text-xs text-slate-500 mb-2')

                    async def save_then_batch():
                        # 先落盘再批量装：否则用户刚在上面把主控地址改了却没保存，
                        # install_probe_on_server 读的还是 ADMIN_CONFIG 里的旧值，
                        # 一整批 agent 会被烧上旧地址，白跑一轮还更难排查。
                        await save_settings(close_dialog=False, quiet=True)
                        open_batch_probe_dialog()

                    ui.button('一键安装 / 更新所有探针', icon='sync', on_click=save_then_batch).props(
                        'flat').classes(theme['save'] + ' w-full')
                    ui.label(
                        f'当前共 {len(SERVERS_CACHE)} 台服务器。只覆盖本面板自己的 agent，'
                        '不影响完整版探针，也不影响 xray / x-ui 等代理服务。'
                    ).classes('text-[11px] text-slate-500 mt-1')

        async def save_settings(*, close_dialog=True, quiet=False):
            # 两个参数都是 keyword-only：NiceGUI 的 handle_event 会检查处理函数
            # 是否有「无默认值的位置参数」来决定要不要把事件对象传进来，
            # keyword-only 参数一律不参与这个判断，所以底部「保存设置」按钮
            # 直接绑定本函数时一定是无参调用，不会把 ClickEventArguments
            # 误塞给 close_dialog。
            url_val = url_input.value.strip().rstrip('/')
            if url_val:
                ADMIN_CONFIG['manager_base_url'] = url_val

            ADMIN_CONFIG['tg_bot_token'] = tg_token.value.strip()
            ADMIN_CONFIG['tg_chat_id'] = tg_id.value.strip()

            await save_admin_config()
            if not quiet:
                safe_notify('✅ 设置已保存（改了主控端外部地址的话，记得用「一键安装 / 更新所有探针」推一遍）', 'positive')
            if close_dialog:
                d.close()

        with ui.row().classes(theme['footer_full']):
            ui.button('保存设置', icon='save', on_click=save_settings).props('flat').classes(theme['save_full'])
    d.open()
