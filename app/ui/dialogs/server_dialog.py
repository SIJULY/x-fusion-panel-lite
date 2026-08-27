import asyncio
import socket

from nicegui import app, run, ui

from app.core.config import AUTO_COUNTRY_MAP, PROBE_AGENT_NAME, PROBE_AGENT_SCRIPT
from app.core.logging import logger
from app.core.state import (
    CURRENT_VIEW_STATE,
    EXPANDED_GROUPS,
    NODES_DATA,
    PROBE_DATA_CACHE,
    SERVERS_CACHE,
    SIDEBAR_UI_REFS,
)
from app.services.probe import install_probe_on_server
from app.services.server_ops import fast_resolve_single_server, generate_smart_name
from app.services.ssh import _ssh_exec_wrapper
from app.storage.repositories import save_servers, save_single_server
from app.ui.common.notifications import safe_notify
from app.ui.components.dashboard import refresh_dashboard_ui
from app.ui.components.sidebar import render_sidebar_content, render_single_sidebar_row
from app.utils.geo import detect_country_group


SINGLE_ROW_COLS = 'grid-template-columns: minmax(0, 3fr) minmax(0, 1fr) minmax(0, 1.5fr) minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr) 140px; align-items: center;'
XHTTP_UNINSTALL_SCRIPT = r"""
#!/bin/bash
systemctl stop xray
systemctl disable xray
rm -f /etc/systemd/system/xray.service
systemctl daemon-reload
rm -rf /usr/local/etc/xray

echo "Xray Service Uninstalled (Binary kept safe)"
"""


SSH_PAGE_TERMINALS = {}


def _sync_resolve_ip(host):
    try:
        return socket.gethostbyname(host)
    except:
        return host


def _server_dialog_theme():
    is_dark = bool(app.storage.user.get('is_dark', True))
    return {
        'is_dark': is_dark,
        'card': 'w-full max-w-sm p-0 gap-0 flex flex-col overflow-hidden rounded-sm bg-[#070b14] border border-[#1e3a5f]/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-full max-w-sm p-0 gap-0 flex flex-col overflow-hidden rounded-sm bg-white border border-slate-300/90 shadow-[0_10px_28px_rgba(148,163,184,0.18)]',
        'header': 'w-full justify-between items-center px-5 py-4 border-b border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14] relative overflow-hidden' if is_dark else 'w-full justify-between items-center px-5 py-4 border-b border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff] relative overflow-hidden',
        'icon_box': 'w-9 h-9 rounded-sm flex items-center justify-center bg-[#050b14] border border-[#1e3a5f] shadow-[0_0_8px_rgba(0,0,0,0.7)] text-cyan-400 relative overflow-hidden' if is_dark else 'w-9 h-9 rounded-sm flex items-center justify-center bg-sky-50 border border-slate-300 shadow-[0_4px_12px_rgba(148,163,184,0.14)] text-sky-600 relative overflow-hidden',
        'title': 'text-lg font-black text-slate-100 tracking-wide' if is_dark else 'text-lg font-black text-slate-800 tracking-wide',
        'body': 'w-full gap-2 p-5 bg-[#030712]' if is_dark else 'w-full gap-2 p-5 bg-[#f8fbff]',
        'input': 'outlined dense dark color=cyan standout' if is_dark else 'outlined dense color=blue',
        'select': 'outlined dense dark color=cyan options-dense' if is_dark else 'outlined dense color=blue options-dense',
        'panel_bg': 'w-full animated fadeIn bg-[#030712] text-slate-200 px-5 pb-5' if is_dark else 'w-full animated fadeIn bg-[#f8fbff] text-slate-700 px-5 pb-5',
        'empty_box': 'w-full h-48 justify-center items-center bg-[#050b14] rounded-sm border border-dashed border-[#1e3a5f]/55' if is_dark else 'w-full h-48 justify-center items-center bg-sky-50 rounded-sm border border-dashed border-slate-300',
        'btn_primary': 'bg-cyan-950/45 text-cyan-300 border border-cyan-500/45 hover:bg-cyan-900/55 hover:shadow-[0_0_12px_rgba(34,211,238,0.32)] px-4 py-1 rounded-sm font-black tracking-wide transition-all' if is_dark else 'bg-sky-100 text-sky-700 border border-sky-300 hover:bg-sky-200 px-4 py-1 rounded-sm font-black tracking-wide transition-all',
        'btn_delete': 'bg-rose-950/45 text-rose-300 border border-rose-500/45 hover:bg-rose-900/55 hover:shadow-[0_0_12px_rgba(244,63,94,0.28)] w-full rounded-sm font-black tracking-wide transition-all' if is_dark else 'bg-rose-100 text-rose-700 border border-rose-300 hover:bg-rose-200 w-full rounded-sm font-black tracking-wide transition-all',
        'btn_confirm': 'bg-rose-950/45 text-rose-300 border border-rose-500/45 hover:bg-rose-900/55 hover:shadow-[0_0_12px_rgba(244,63,94,0.28)] rounded-sm font-black tracking-wide transition-all' if is_dark else 'bg-rose-100 text-rose-700 border border-rose-300 hover:bg-rose-200 rounded-sm font-black tracking-wide transition-all',
        'close_btn': 'text-slate-400 hover:text-cyan-300 hover:bg-cyan-950/30' if is_dark else 'text-slate-500 hover:text-sky-700 hover:bg-sky-100',
        'outline_btn': 'text-slate-300 border-slate-600 hover:bg-slate-800/40 text-xs font-bold tracking-wide rounded-sm' if is_dark else 'text-slate-600 border-slate-300 hover:bg-slate-100 text-xs font-bold tracking-wide rounded-sm',
    }


async def save_server_config(server_data, is_add=True, idx=None):
    client = None
    try:
        client = ui.context.client
    except:
        pass

    logger.debug(f"[SaveServerDialog] save_server_config called | is_add={is_add} idx={idx} client_present={client is not None} servers_before={len(SERVERS_CACHE)} url={server_data.get('url')} name={server_data.get('name')}")

    if not server_data.get('name') or not server_data.get('url'):
        safe_notify("名称和地址不能为空", "negative")
        return False

    old_group = None
    if not is_add and idx is not None and 0 <= idx < len(SERVERS_CACHE):
        old_group = SERVERS_CACHE[idx].get('group')

    if is_add:
        for s in SERVERS_CACHE:
            if s['url'] == server_data['url']:
                safe_notify("已存在！", "warning")
                return False

        has_flag = False
        for v in AUTO_COUNTRY_MAP.values():
            if v.split(' ')[0] in server_data['name']:
                has_flag = True
                break
        if not has_flag and '🏳️' not in server_data['name']:
            server_data['name'] = f"🏳️ {server_data['name']}"

        SERVERS_CACHE.append(server_data)
        safe_notify(f"已添加: {server_data['name']}", "positive")
    else:
        if idx is not None and 0 <= idx < len(SERVERS_CACHE):
            SERVERS_CACHE[idx].update(server_data)
            safe_notify(f"已更新: {server_data['name']}", "positive")
        else:
            safe_notify("目标不存在", "negative")
            return False

    await save_single_server(server_data)
    logger.debug(f"[SaveServerDialog] save_single_server done | servers_after={len(SERVERS_CACHE)} rows_refs={len(SIDEBAR_UI_REFS.get('rows', {}))} group_refs={len(SIDEBAR_UI_REFS.get('groups', {}))}")

    new_group = server_data.get('group', '默认分组')
    if new_group in ['默认分组', '自动注册', '未分组', '自动导入']:
        try:
            new_group = detect_country_group(server_data.get('name', ''), server_data)
        except:
            pass
        if not new_group:
            new_group = '🏳️ 其他地区'

    need_full_refresh = False

    try:
        if is_add:
            if new_group in SIDEBAR_UI_REFS['groups']:
                with SIDEBAR_UI_REFS['groups'][new_group]:
                    render_single_sidebar_row(server_data)
                EXPANDED_GROUPS.add(new_group)
            else:
                need_full_refresh = True
        elif old_group != new_group:
            row_el = SIDEBAR_UI_REFS['rows'].get(server_data['url'])
            target_col = SIDEBAR_UI_REFS['groups'].get(new_group)
            if row_el and target_col:
                row_el.move(target_col)
                EXPANDED_GROUPS.add(new_group)
            else:
                need_full_refresh = True
    except Exception as e:
        logger.error(f"UI Move Error: {e}")
        need_full_refresh = True

    logger.debug(f"[SaveServerDialog] sidebar refresh decision | need_full_refresh={need_full_refresh} new_group={new_group} rows_refs={len(SIDEBAR_UI_REFS.get('rows', {}))} group_refs={len(SIDEBAR_UI_REFS.get('groups', {}))}")
    if need_full_refresh:
        try:
            logger.debug(f"[SaveServerDialog] calling render_sidebar_content.refresh | client_present={client is not None}")
            if client:
                with client:
                    render_sidebar_content.refresh()
            else:
                render_sidebar_content.refresh()
            logger.debug("[SaveServerDialog] render_sidebar_content.refresh returned")
        except Exception as e:
            logger.error(f"[SaveServerDialog] render_sidebar_content.refresh failed: {e}")

    current_scope = CURRENT_VIEW_STATE.get('scope')
    current_data = CURRENT_VIEW_STATE.get('data')

    if current_scope == 'SINGLE' and (current_data == server_data or (is_add and server_data == SERVERS_CACHE[-1])):
        try:
            from app.ui.pages.content_router import refresh_content

            await refresh_content('SINGLE', server_data, force_refresh=True)
        except:
            pass
    elif current_scope in ['ALL', 'TAG', 'COUNTRY']:
        CURRENT_VIEW_STATE['scope'] = None
        try:
            from app.ui.pages.content_router import refresh_content

            await refresh_content(current_scope, current_data, force_refresh=True)
        except:
            pass
    elif current_scope == 'DASHBOARD':
        try:
            logger.debug(f"[SaveServerDialog] calling refresh_dashboard_ui | client_present={client is not None} current_scope={current_scope}")
            if client:
                with client:
                    await refresh_dashboard_ui()
            else:
                await refresh_dashboard_ui()
            logger.debug("[SaveServerDialog] refresh_dashboard_ui returned")
        except Exception as e:
            logger.error(f"[SaveServerDialog] refresh_dashboard_ui failed: {e}")

    asyncio.create_task(fast_resolve_single_server(server_data))

    return True


async def open_server_dialog(idx=None):
    is_edit = idx is not None
    original_data = SERVERS_CACHE[idx] if is_edit else {}
    data = original_data.copy()

    theme = _server_dialog_theme()

    with ui.dialog() as d, ui.card().classes(theme['card']):
        with ui.column().classes(theme['header'].replace('items-center', 'items-stretch') + ' gap-3'):
            ui.element('div').classes('absolute inset-0 bg-[url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0IiBoZWlnaHQ9IjQiPjxyZWN0IHdpZHRoPSI0IiBoZWlnaHQ9IjQiIGZpbGw9InRyYW5zcGFyZW50Ii8+PHJlY3Qgd2lkdGg9IjEiIGhlaWdodD0iMSIgZmlsbD0icmdiYSgzNCwyMTEsMjM4LDAuMDcpIi8+PC9zdmc+")] opacity-100 pointer-events-none')
            with ui.row().classes('w-full justify-between items-start z-10'):
                with ui.row().classes('items-center gap-3'):
                    with ui.element('div').classes(theme['icon_box']):
                        ui.element('div').classes('absolute inset-0 bg-cyan-400/10')
                        ui.icon('dns').classes('text-[18px] drop-shadow-[0_0_5px_currentColor]')
                    ui.label('编辑服务器' if is_edit else '添加服务器').classes(theme['title'])
                ui.button(icon='close', on_click=d.close).props('flat round dense color=grey').classes(theme['close_btn'] + ' z-10')
            with ui.row().classes('items-center gap-2 z-10 text-cyan-400' if theme['is_dark'] else 'items-center gap-2 z-10 text-sky-600'):
                ui.icon('terminal').classes('text-[16px]')
                ui.label('SSH / 探针模式').classes('text-xs font-black tracking-wide')

        async def save_basic_info_only():
            if not is_edit:
                safe_notify("新增服务器请使用下方的保存按钮", "warning")
                return

            new_name = name_input.value.strip()
            new_group = group_input.value
            new_cf_domain = cf_primary_domain_input.value.strip()

            if not new_name:
                new_name = await generate_smart_name(data)

            SERVERS_CACHE[idx]['name'] = new_name
            SERVERS_CACHE[idx]['group'] = new_group
            SERVERS_CACHE[idx]['cf_primary_domain'] = new_cf_domain

            await save_single_server(SERVERS_CACHE[idx])
            render_sidebar_content.refresh()

            current_scope = CURRENT_VIEW_STATE.get('scope')
            if current_scope == 'SINGLE' and CURRENT_VIEW_STATE.get('data') == SERVERS_CACHE[idx]:
                try:
                    from app.ui.pages.content_router import refresh_content

                    await refresh_content('SINGLE', SERVERS_CACHE[idx])
                except:
                    pass
            elif current_scope in ['ALL', 'TAG', 'COUNTRY']:
                CURRENT_VIEW_STATE['scope'] = None
                try:
                    from app.ui.pages.content_router import refresh_content

                    await refresh_content(current_scope, CURRENT_VIEW_STATE.get('data'), force_refresh=False)
                except:
                    pass

            safe_notify("✅ 基础信息已更新", "positive")
            d.close()

        with ui.column().classes(theme['body']):
            name_input = ui.input(value=data.get('name', ''), label='备注名称 (留空自动获取)').classes('w-full').props(theme['input'])
            cf_primary_domain_input = ui.input(value=data.get('cf_primary_domain', ''), label='Cloudflare 主域名 (选填，自动同步节点/IP)').classes('w-full').props(theme['input'])

            with ui.row().classes('w-full items-center gap-2 no-wrap'):
                from app.services.server_ops import get_all_groups

                group_input = ui.select(options=get_all_groups(), value=data.get('group', '默认分组'), new_value_mode='add-unique', label='分组').classes('flex-grow').props(theme['select'])

                if is_edit:
                    ui.button(icon='save', on_click=save_basic_info_only).props('flat dense round').classes(theme['close_btn']).tooltip('仅保存信息 (不重新部署)')

        inputs = {}
        btn_keycap_blue = theme['btn_primary']
        btn_keycap_delete = theme['btn_delete']
        btn_keycap_red_confirm = theme['btn_confirm']

        async def save_ssh_data():
            final_name = name_input.value.strip()
            final_group = group_input.value
            final_cf_domain = cf_primary_domain_input.value.strip()
            new_server_data = data.copy()
            new_server_data['group'] = final_group
            new_server_data['cf_primary_domain'] = final_cf_domain

            if not inputs.get('ssh_host'):
                return
            s_host = inputs['ssh_host'].value.strip()
            if not s_host:
                safe_notify("SSH 主机 IP 不能为空", "negative")
                return

            s_port = str(inputs['ssh_port'].value).strip() or '22'

            new_server_data.update({
                'ssh_host': s_host,
                'ssh_port': s_port,
                'ssh_user': inputs['ssh_user'].value.strip(),
                'ssh_auth_type': inputs['auth_type'].value,
                'ssh_password': inputs['ssh_pwd'].value if inputs['ssh_pwd'] else '',
                'ssh_key': inputs['ssh_key'].value if inputs['ssh_key'] else '',
                'probe_installed': True,
            })

            if not new_server_data.get('url'):
                new_server_data['url'] = f"http://{s_host}:{s_port}"

            if not final_name:
                safe_notify("正在生成名称...", "ongoing")
                final_name = await generate_smart_name(new_server_data)
            new_server_data['name'] = final_name

            success = await save_server_config(new_server_data, is_add=not is_edit, idx=idx)

            if success:
                data.update(new_server_data)

                safe_notify("🚀 配置已保存，正在自动推送探针...", "ongoing")
                target_for_install = new_server_data
                if is_edit and idx is not None and 0 <= idx < len(SERVERS_CACHE):
                    target_for_install = SERVERS_CACHE[idx]
                elif not is_edit:
                    target_for_install = next((s for s in SERVERS_CACHE if s.get('url') == new_server_data.get('url')), new_server_data)

                async def _install_and_report(target_server):
                    ok = await install_probe_on_server(target_server)
                    if ok:
                        safe_notify("✅ 探针安装成功，等待首次上报", "positive")
                    else:
                        target_server['probe_installed'] = False
                        await save_servers()
                        safe_notify("⚠️ 探针安装失败，请检查 SSH 凭据、sudo/root 权限以及主控端地址", "warning")
                asyncio.create_task(_install_and_report(target_for_install))

        with ui.column().classes(theme['panel_bg'] + ' gap-3'):
            init_host = data.get('ssh_host')
            if not init_host and is_edit:
                if '://' in data.get('url', ''):
                    init_host = data.get('url', '').split('://')[-1].split(':')[0]
                else:
                    init_host = data.get('url', '').split(':')[0]

            inputs['ssh_host'] = ui.input(label='SSH 主机 IP', value=init_host).classes('w-full').props(theme['input'])

            with ui.column().classes('w-full gap-3'):
                with ui.row().classes('w-full gap-2'):
                    inputs['ssh_user'] = ui.input(value=data.get('ssh_user', 'root'), label='SSH 用户').classes('flex-1').props(theme['input'])
                    inputs['ssh_port'] = ui.input(value=data.get('ssh_port', '22'), label='端口').classes('w-1/3').props(theme['input'])

                valid_auth_options = ['全局密钥', '独立密码', '独立密钥']
                current_auth = data.get('ssh_auth_type', '全局密钥')
                if current_auth not in valid_auth_options:
                    current_auth = '全局密钥'

                inputs['auth_type'] = ui.select(valid_auth_options, value=current_auth, label='认证方式').classes('w-full').props(theme['select'])
                inputs['ssh_pwd'] = ui.input(label='SSH 密码', password=True, value=data.get('ssh_password', '')).classes('w-full').props(theme['input'])
                inputs['ssh_pwd'].bind_visibility_from(inputs['auth_type'], 'value', value='独立密码')

                # 修复点：移除了 props 里的 bg-color="[#050b14]"
                inputs['ssh_key'] = ui.textarea(label='SSH 私钥', value=data.get('ssh_key', '')).classes('w-full').props('outlined dense rows=3 input-class=font-mono text-xs dark color=cyan standout' if theme['is_dark'] else 'outlined dense rows=3 input-class=font-mono text-xs color=blue')
                inputs['ssh_key'].bind_visibility_from(inputs['auth_type'], 'value', value='独立密钥')

            ui.separator().classes('my-1')
            with ui.row().classes('w-full justify-between items-center'):
                ui.label('✅ 自动使用全局私钥').bind_visibility_from(inputs['auth_type'], 'value', value='全局密钥').classes('text-emerald-400 text-xs font-bold')
                ui.element('div').bind_visibility_from(inputs['auth_type'], 'value', value='独立密码')
                ui.element('div').bind_visibility_from(inputs['auth_type'], 'value', value='独立密钥')
                ui.button('保存 SSH', icon='save', on_click=save_ssh_data).props('flat').classes(btn_keycap_blue)

        if is_edit:
            with ui.row().classes('w-full justify-start mt-4 pt-2 border-t border-[#1e3a5f]/35'):
                async def open_delete_confirm():
                    with ui.dialog() as del_d, ui.card().classes('w-[360px] p-0 gap-0 overflow-hidden rounded-sm bg-[#070b14] border border-rose-800/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if theme['is_dark'] else 'w-[360px] p-0 gap-0 overflow-hidden rounded-sm bg-white border border-rose-300 shadow-[0_10px_28px_rgba(148,163,184,0.18)]'):
                        with ui.column().classes('w-full p-5 gap-3 bg-gradient-to-r from-[#19070d] to-[#0b0911] border-b border-rose-900/60' if theme['is_dark'] else 'w-full p-5 gap-3 bg-gradient-to-r from-rose-50 to-orange-50 border-b border-rose-200'):
                            ui.label('删除确认').classes('text-lg font-black text-rose-300 tracking-wide' if theme['is_dark'] else 'text-lg font-black text-rose-700 tracking-wide')
                            ui.label('将从面板中彻底移除该服务器：').classes('text-sm text-slate-400' if theme['is_dark'] else 'text-sm text-slate-600')
                        with ui.column().classes('w-full p-4 gap-2 bg-[#030712]' if theme['is_dark'] else 'w-full p-4 gap-2 bg-white'):
                            has_probe = data.get('probe_installed', False)

                            target_label = data.get('name') or data.get('ssh_host') or data.get('url') or ''
                            ui.label(f'🖥️ {target_label}').classes('text-sm font-bold text-slate-200' if theme['is_dark'] else 'text-sm font-bold text-slate-700')
                            ui.label('将删除该服务器的 SSH 连接信息、节点缓存与探针数据。').classes('text-xs text-slate-500')

                            chk_uninstall = ui.checkbox('同时卸载远程探针脚本', value=True).classes('text-sm font-bold text-rose-400')
                            chk_uninstall.set_visibility(has_probe)

                            async def confirm_execution():
                                if idx >= len(SERVERS_CACHE):
                                    return
                                target_srv = SERVERS_CACHE[idx]
                                will_uninstall = chk_uninstall.value and chk_uninstall.visible

                                if will_uninstall:
                                    loading_notify = ui.notification('正在尝试连接并卸载探针...', timeout=None, spinner=True)
                                    try:
                                        # 只卸载本面板自己的探针（x-fusion-agent-lite），绝不去动
                                        # 完整版的 x-fusion-agent——同一台 VPS 上可能两个面板的
                                        # 探针并存，把别人的删了就等于把用户另一个面板搞断线。
                                        uninstall_cmd = (
                                            f"systemctl stop {PROBE_AGENT_NAME}"
                                            f" && systemctl disable {PROBE_AGENT_NAME}"
                                            f" && rm -f /etc/systemd/system/{PROBE_AGENT_NAME}.service"
                                            f" && systemctl daemon-reload"
                                            f" && rm -f {PROBE_AGENT_SCRIPT}"
                                        )
                                        # _ssh_exec_wrapper 是 async 函数，直接 await。
                                        # 原来写的是 run.io_bound(lambda: _ssh_exec_wrapper(...))：
                                        # io_bound 会把 lambda 丢到线程池里执行，而 lambda 调用一个
                                        # async 函数只是「造出一个协程对象」就立刻返回，协程从未被
                                        # 执行过。于是这里拿到的是协程对象而不是 (success, output)
                                        # 元组，解包直接抛 TypeError: cannot unpack non-iterable
                                        # coroutine object，整个 confirm_execution 中断在这一行，
                                        # 下面的 SERVERS_CACHE.pop(idx) 永远走不到——表现就是
                                        # 「勾了卸载探针的服务器删不掉」。
                                        success, output = await _ssh_exec_wrapper(target_srv, uninstall_cmd)
                                        if success:
                                            ui.notify('✅ 远程探针已卸载清理', type='positive')
                                        else:
                                            ui.notify('⚠️ 远程卸载失败 (可能是连接超时)，将仅删除本地记录', type='warning')
                                    except Exception as e:
                                        # 卸载只是「顺手清理远端」，失败绝不能拦住本地删除，
                                        # 否则机器一旦连不上就永远删不掉了。
                                        logger.warning(f"⚠️ [删除服务器] 远程卸载探针异常，仅删除本地记录: {e}")
                                        ui.notify(f'⚠️ 远程卸载异常（{e}），将仅删除本地记录', type='warning')
                                    finally:
                                        loading_notify.dismiss()

                                SERVERS_CACHE.pop(idx)
                                u = target_srv.get('url')
                                p_u = target_srv.get('ssh_host') or u
                                for k in [u, p_u]:
                                    if k in PROBE_DATA_CACHE:
                                        del PROBE_DATA_CACHE[k]
                                    if k in NODES_DATA:
                                        del NODES_DATA[k]
                                safe_notify('✅ 服务器已彻底删除', 'positive')

                                await save_servers()
                                del_d.close()
                                d.close()
                                render_sidebar_content.refresh()
                                current_scope = CURRENT_VIEW_STATE.get('scope')
                                current_data = CURRENT_VIEW_STATE.get('data')

                                from app.ui.pages.content_router import content_container, refresh_content

                                if current_scope == 'SINGLE' and current_data == target_srv:
                                    content_container.clear()
                                    with content_container:
                                        ui.label('该服务器已删除').classes('text-gray-400 text-lg w-full text-center mt-20')
                                elif current_scope in ['ALL', 'TAG', 'COUNTRY']:
                                    CURRENT_VIEW_STATE['scope'] = None
                                    await refresh_content(current_scope, current_data, force_refresh=False)

                            with ui.row().classes('w-full justify-end mt-4 gap-2'):
                                ui.button('取消', on_click=del_d.close).props('outline color=grey').classes(theme['outline_btn'])
                                ui.button('确认执行', on_click=confirm_execution).props('flat').classes(btn_keycap_red_confirm)
                    del_d.open()

                ui.button('删除服务器 / 卸载探针', icon='delete', on_click=open_delete_confirm).props('flat').classes(btn_keycap_delete)

    d.open()


def cleanup_ssh_route_terminal(server_key=None):
    keys = [server_key] if server_key else list(SSH_PAGE_TERMINALS.keys())
    for key in keys:
        inst = SSH_PAGE_TERMINALS.pop(key, None)
        try:
            if inst:
                inst.close()
        except:
            pass
