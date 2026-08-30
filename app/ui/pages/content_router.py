import asyncio
import time

from nicegui import app, ui

from app.core.logging import logger, scrub

from app.core.config import PAGE_SIZE
from app.core.state import CURRENT_VIEW_STATE, LAST_SYNC_MAP, REFRESH_LOCKS, SERVERS_CACHE
from app.services.xui_fetch import fetch_inbounds_safe
from app.services.probe import list_offline_servers
from app.ui.common.notifications import safe_notify
from app.utils.formatters import smart_sort_key
from app.utils.geo import detect_country_group
from app.services.subscriptions import copy_group_link


content_container = None


def _match_server_search(server, keyword):
    keyword = str(keyword or '').strip().lower()
    if not keyword:
        return True

    name = str(server.get('name', '') or '').lower()
    url = str(server.get('url', '') or '').lower()
    ssh_host = str(server.get('ssh_host', '') or '').lower()

    host_from_url = url.split('://')[-1].split('/')[0].split(':')[0] if url else ''
    search_pool = [name, url, ssh_host, host_from_url]
    return any(keyword in field for field in search_pool if field)


def _persist_last_view(scope, data, page_num=1):
    try:
        stored_data = data
        if scope in ['SINGLE', 'SSH_SINGLE'] and isinstance(data, dict):
            stored_data = data.get('url')
        app.storage.user['last_view_scope'] = scope
        app.storage.user['last_view_data'] = stored_data
        app.storage.user['last_view_page'] = page_num
    except:
        pass


def get_targets_by_scope(scope, data):
    targets = []
    try:
        if scope == 'ALL':
            targets = list(SERVERS_CACHE)
        elif scope == 'OFFLINE':
            # 固定分组「离线服务器」。这是个筛选视图而不是归属，机器同时还在自己的
            # 区域分组里；判据见 probe.is_server_offline（未装探针的不算离线）。
            targets = list_offline_servers()
        elif scope == 'TAG':
            targets = [s for s in SERVERS_CACHE if data in s.get('tags', [])]
        elif scope == 'COUNTRY':
            for s in SERVERS_CACHE:
                saved = s.get('group')
                real = saved if saved and saved not in ['默认分组', '自动注册', '未分组', '自动导入', '🏳️ 其他地区'] else detect_country_group(s.get('name', ''))
                if real == data:
                    targets.append(s)
        elif scope in ['SINGLE', 'SSH_SINGLE']:
            if data in SERVERS_CACHE:
                targets = [data]
            elif isinstance(data, dict):
                data_url = data.get('url')
                if data_url:
                    matched = next((s for s in SERVERS_CACHE if s.get('url') == data_url), None)
                    if matched:
                        targets = [matched]
    except:
        pass
    return targets


async def refresh_content(scope='ALL', data=None, force_refresh=False, sync_name_action=False, page_num=1, manual_client=None):
    global CURRENT_VIEW_STATE, REFRESH_LOCKS, LAST_SYNC_MAP

    client = manual_client
    if not client:
        try:
            client = ui.context.client
        except:
            pass
    logger.debug(f"[ContentRouter] refresh_content called | scope={scope} data={scrub(data)} force_refresh={force_refresh} page_num={page_num} client_present={client is not None} container_present={content_container is not None} current_view_before={scrub(CURRENT_VIEW_STATE)}")
    if not client:
        logger.debug("[ContentRouter] refresh_content abort | no client")
        return

    with client:
        cache_key = f"{scope}::{data}::P{page_num}"
        lock_key = cache_key

        now = time.time()

        targets = get_targets_by_scope(scope, data)
        logger.debug(f"[ContentRouter] refresh_content targets_resolved | scope={scope} data={scrub(data)} targets={len(targets)}")
        start_idx = (page_num - 1) * PAGE_SIZE
        end_idx = start_idx + PAGE_SIZE
        current_page_servers = targets[start_idx:end_idx] if targets else []

        has_probe = any(s.get('probe_installed') for s in current_page_servers)
        has_api_only = any(not s.get('probe_installed') for s in current_page_servers)

        is_all_probe = has_probe and not has_api_only

        if not force_refresh and is_all_probe:
            CURRENT_VIEW_STATE.update({'scope': scope, 'data': data, 'page': page_num, 'render_token': now})
            _persist_last_view(scope, data, page_num)
            logger.debug(f"[ContentRouter] refresh_content using probe realtime path | current_view={scrub(CURRENT_VIEW_STATE)}")
            await _render_ui_internal(scope, data, page_num, force_refresh, sync_name_action, client)
            safe_notify("⚡ 实时数据 (探针推送)", "positive", timeout=1000)
            return

        if lock_key in REFRESH_LOCKS:
            logger.debug(f"[ContentRouter] refresh_content abort | lock_key active={lock_key}")
            return

        CURRENT_VIEW_STATE.update({'scope': scope, 'data': data, 'page': page_num, 'render_token': now})
        _persist_last_view(scope, data, page_num)
        logger.debug(f"[ContentRouter] refresh_content state updated | current_view={scrub(CURRENT_VIEW_STATE)}")

        await _render_ui_internal(scope, data, page_num, force_refresh, sync_name_action, client)
        logger.debug(f"[ContentRouter] refresh_content initial render done | scope={scope} data={scrub(data)} current_view_after={scrub(CURRENT_VIEW_STATE)}")

        if not current_page_servers:
            return

        async def _background_fetch(token_at_start):
            REFRESH_LOCKS.add(lock_key)
            try:
                # 自动路径（切分组、翻页）只同步没装探针的机器——探针机吃缓存。
                # 但用户按下刷新按钮时 force_refresh=True，那就该真刷：推送间隔
                # 已经是半小时级的，缓存不再代表现状，而「主动点刷新」正是该付
                # SSH 开销的时机。这也是「显式动作才走 SSH」这条原则的另一半。
                if force_refresh:
                    sync_targets = list(current_page_servers)
                else:
                    sync_targets = [s for s in current_page_servers if not s.get('probe_installed')]

                if sync_targets:
                    logger.debug(f"[ContentRouter] auto background sync current page | scope={scope} data={scrub(data)} page_num={page_num} force={force_refresh} targets={len(sync_targets)}")
                    with client:
                        safe_notify(f"🔄 后台同步当前页 {len(sync_targets)} 台节点...", "ongoing", timeout=1200)

                    tasks = [fetch_inbounds_safe(s, force_refresh=True, sync_name=sync_name_action) for s in sync_targets]
                    await asyncio.gather(*tasks, return_exceptions=True)

                    if CURRENT_VIEW_STATE.get('render_token') == token_at_start:
                        with client:
                            await _render_ui_internal(scope, data, page_num, force_refresh, sync_name_action, client)
                            LAST_SYNC_MAP[cache_key] = time.time()
                            safe_notify("✅ 当前页后台同步完成", "positive", timeout=1000)
                            try:
                                from app.ui.components.sidebar import render_sidebar_content

                                render_sidebar_content.refresh()
                            except:
                                pass
                else:
                    LAST_SYNC_MAP[cache_key] = time.time()
            finally:
                REFRESH_LOCKS.discard(lock_key)

        asyncio.create_task(_background_fetch(now))


async def _render_ui_internal(scope, data, page_num, force_refresh, sync_name_action, client):
    logger.debug(f"[ContentRouter] _render_ui_internal called | scope={scope} data={scrub(data)} page_num={page_num} container_present={content_container is not None} container_id={id(content_container) if content_container else None}")
    if content_container:
        content_container.clear()
        content_container.classes(remove='justify-center items-center overflow-hidden p-6', add='overflow-y-auto p-4 pl-6 justify-start')
        with content_container:
            targets = get_targets_by_scope(scope, data)
            logger.debug(f"[ContentRouter] _render_ui_internal targets | scope={scope} data={scrub(data)} targets={len(targets)}")
            from app.ui.dialogs.server_dialog import cleanup_ssh_route_terminal

            if scope == 'SSH_SINGLE':
                if targets:
                    from app.ui.pages.single_ssh import render_single_ssh_view

                    logger.debug(f"[ContentRouter] _render_ui_internal branch=SSH_SINGLE found target | url={targets[0].get('url')}")
                    await render_single_ssh_view(targets[0])
                    return
                else:
                    logger.debug("[ContentRouter] _render_ui_internal branch=SSH_SINGLE target missing")
                    ui.label('服务器未找到')
                    return

            cleanup_ssh_route_terminal()

            if scope == 'SINGLE':
                if targets:
                    from app.ui.pages.single_server import render_single_server_view

                    logger.debug(f"[ContentRouter] _render_ui_internal branch=SINGLE found target | url={targets[0].get('url')}")
                    await render_single_server_view(targets[0])
                    return
                else:
                    logger.debug("[ContentRouter] _render_ui_internal branch=SINGLE target missing")
                    ui.label('服务器未找到')
                    return

            title = ""
            is_group_view = False
            hide_group_column = False
            if scope == 'ALL':
                title = f"🌍 所有服务器 ({len(targets)})"
            elif scope == 'OFFLINE':
                title = f"🔴 离线服务器 ({len(targets)})"
            elif scope == 'TAG':
                title = f"🏷️ 自定义分组: {data} ({len(targets)})"
                is_group_view = True
            elif scope == 'COUNTRY':
                title = f"🏳️ 区域: {data} ({len(targets)})"
                is_group_view = True
                # 区域视图里每一行的「所在组」都相同，换成「在线状态 / IP」更有信息量
                hide_group_column = True

            logger.debug(f"[ContentRouter] _render_ui_internal branch={scope} title={title}")
            search_state = {'keyword': ''}

            with ui.row().classes('items-center w-full mb-4 border-b pb-2 justify-between gap-4'):
                with ui.row().classes('items-center gap-4 flex-wrap'):
                    ui.label(title).classes('text-2xl font-bold')
                with ui.row().classes('items-center gap-2 flex-wrap justify-end'):
                    if is_group_view and targets:
                        with ui.row().classes('gap-1'):
                            ui.button(icon='content_copy', on_click=lambda: copy_group_link(data)).props('flat dense round size=sm color=grey')
                            ui.button(icon='bolt', on_click=lambda: copy_group_link(data, target='surge')).props('flat dense round size=sm text-color=orange')
                            ui.button(icon='cloud_queue', on_click=lambda: copy_group_link(data, target='clash')).props('flat dense round size=sm text-color=green')
                    if scope == 'ALL':
                        ui.input(
                            placeholder='搜索服务器名称或 IP',
                            on_change=lambda e: [search_state.__setitem__('keyword', e.value or ''), render_target_list.refresh()],
                        ).props('outlined dense clearable').classes('w-[320px] max-w-full')

            @ui.refreshable
            async def render_target_list():
                filtered_targets = list(targets)
                keyword = search_state['keyword'].strip()

                if keyword:
                    filtered_targets = [s for s in targets if _match_server_search(s, keyword)]
                    with ui.row().classes('w-full items-center justify-between mb-2 px-1'):
                        ui.label(f'匹配到 {len(filtered_targets)} 台服务器').classes('text-sm font-bold text-slate-500')

                if not filtered_targets:
                    logger.debug(f"[ContentRouter] _render_ui_internal empty list | scope={scope} data={scrub(data)} keyword={keyword}")
                    with ui.column().classes('w-full h-64 justify-center items-center text-gray-400'):
                        # 离线分组空着是好事，别用「列表为空」这种像出错的说法
                        if scope == 'OFFLINE' and not keyword:
                            ui.icon('check_circle', size='4rem').classes('text-emerald-400')
                            ui.label('当前没有检测到离线服务器')
                        else:
                            ui.icon('inbox', size='4rem')
                            ui.label('未找到匹配的服务器' if keyword else '列表为空')
                    return

                try:
                    filtered_targets.sort(key=smart_sort_key)
                except:
                    pass

                from app.ui.pages.aggregated_view import render_aggregated_view

                logger.debug(f"[ContentRouter] _render_ui_internal render_aggregated_view | scope={scope} data={scrub(data)} filtered_targets={len(filtered_targets)} hide_group_column={hide_group_column} keyword={keyword}")
                await render_aggregated_view(filtered_targets, hide_group_column=hide_group_column, initial_page=(1 if keyword else page_num))

            await render_target_list()
    else:
        logger.debug("[ContentRouter] _render_ui_internal abort | content_container missing")
