import asyncio
import json

from fastapi import Request
from nicegui import app, ui

from app.core.logging import logger, scrub

from app.core import state
from app.core.state import (
    ADMIN_CONFIG,
    CURRENT_VIEW_STATE,
    EXPANDED_GROUPS,
    SERVERS_CACHE,
    SIDEBAR_UI_REFS,
)
from app.services.probe import count_unmonitored_servers, get_unmonitored_servers, list_offline_servers, probe_offline_after
from app.storage.repositories import save_admin_config
from app.ui.common.dialogs_data import open_data_mgmt_dialog, open_global_settings_dialog
from app.ui.common.dialogs_settings import open_cloudflare_settings_dialog, open_probe_settings_dialog
from app.ui.dialogs.batch_ssh import BatchSSH
from app.ui.dialogs.bulk_edit import open_bulk_edit_dialog
from app.ui.dialogs.group_dialogs import (
    open_combined_group_management,
    open_quick_group_create_dialog,
)
from app.utils.formatters import smart_sort_key
from app.utils.geo import detect_country_group

batch_ssh_manager = BatchSSH()

# ── 固定分组「离线服务器」 ──
# 它不是用户建的分组：不能改名、不能删、不参与拖拽排序，也不写进 custom_groups。
# EXPANDED_GROUPS / SIDEBAR_UI_REFS 都以分组名为键，所以这里用一个不可能和用户
# 分组撞名的哨兵键——万一用户真建了个叫「离线服务器」的自定义分组，两边不会互相
# 顶掉展开状态。
OFFLINE_GROUP_KEY = '__xf_offline__'
OFFLINE_GROUP_LABEL = '离线服务器'
OFFLINE_ACCENT = '#f43f5e'  # rose-500，和单机详情页的 Offline 徽章同色

# 上一次渲染时看到的离线 URL 集合。
# 离线判定纯粹是「探针多久没推」，时间一到自己就变了，没有任何事件会通知面板，
# 所以只能定时自查。这份快照在 render_sidebar_content 里更新，让定时器能判断
# 「现在的离线集合和屏幕上画的是不是同一份」。
_LAST_OFFLINE_SNAPSHOT = {'urls': None}


def refresh_sidebar_if_offline_changed():
    """定时自查：只有离线集合真的变了才重建侧边栏。

    扫一遍内存缓存是微秒级的，而 render_sidebar_content.refresh() 会重建**所有**
    已连接客户端的侧边栏。无条件每分钟来一次，在软路由上就是白烧 CPU。
    """
    try:
        urls = frozenset(s.get('url') for s in list_offline_servers())
    except Exception as e:
        logger.warning(f'[Sidebar] 离线自查失败: {e}')
        return

    if _LAST_OFFLINE_SNAPSHOT['urls'] == urls:
        return

    was = _LAST_OFFLINE_SNAPSHOT['urls']
    _LAST_OFFLINE_SNAPSHOT['urls'] = urls
    logger.info(f"🔴 [离线分组] 离线机器有变动 ({len(was or ())} → {len(urls)} 台)，重建侧边栏")
    try:
        render_sidebar_content.refresh()
    except Exception as e:
        logger.warning(f'[Sidebar] 离线变动后 refresh 失败: {e}')


async def _save_sidebar_group_order(kind: str, order: list[str]):
    if kind == 'custom':
        current = ADMIN_CONFIG.get('custom_groups', [])
        ADMIN_CONFIG['custom_groups'] = [name for name in order if name in current]
    elif kind == 'region':
        ADMIN_CONFIG['group_order'] = list(order)
    else:
        raise ValueError(f'unknown reorder kind: {kind}')

    await save_admin_config()


@app.post('/api/sidebar/reorder')
async def api_sidebar_reorder(request: Request):
    try:
        payload = await request.json()
        kind = str(payload.get('kind', '')).strip()
        order = payload.get('order', [])
        if kind not in {'custom', 'region'}:
            return {'ok': False, 'msg': 'invalid kind'}
        if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
            return {'ok': False, 'msg': 'invalid order'}
        await _save_sidebar_group_order(kind, order)
        return {'ok': True}
    except Exception as e:
        logger.error(f'[SidebarSort] save order failed: {e}')
        return {'ok': False, 'msg': str(e)}


def _sidebar_theme():
    is_dark = bool(app.storage.user.get('is_dark', True))
    return {
        'is_dark': is_dark,
        'btn_keycap_base': 'border rounded-sm transition-all',
        'btn_name_text': '',
        'btn_settings_text': '',
        'top_btn': 'xf-btn-primary w-full h-[44px] border rounded-sm font-bold px-3 py-2 transition-all',
        'top_wrap': 'w-full p-4 border-b flex-shrink-0 relative overflow-hidden',
        'logo_text': 'hidden',
        'title': 'text-sm font-black tracking-widest uppercase z-10',
        'ip_wrap': 'items-center gap-1 px-2 py-0.5 rounded-sm border z-10',
        'ip_label': 'text-[11px] font-bold',
        'ip_value': 'text-[11px] font-mono font-bold',
        'scroll_wrap': 'w-full flex-grow overflow-y-auto p-2 gap-2',
        'group_action_base': 'flex-grow h-[44px] text-xs font-black rounded-sm border px-3 py-2 tracking-wide transition-all',
        'new_group_btn': '',
        'new_server_btn': '',

        # 保持外层菜单的固定高度和禁止垂直收缩
        'list_item': 'w-full h-[44px] shrink-0 items-center justify-between px-3 border rounded-2xl mb-2 cursor-pointer group transition-all duration-200',

        'list_icon_box': 'xf-sidebar-icon-box w-8 h-8 items-center justify-center rounded-sm border transition-colors shrink-0',
        'list_icon': 'text-sm',
        'list_label': 'font-bold text-sm',
        'section_label': 'text-xs font-bold uppercase tracking-wider',

        # 保持分组外层卡片的固定高度和禁止垂直收缩
        'expansion_custom': 'w-full shrink-0 border rounded-2xl mb-3 transition-all overflow-hidden',
        'expansion_region': 'w-full shrink-0 border rounded-2xl overflow-hidden',

        # 关键修复 1：为右侧的折叠箭头保留 12px 的安全边距（padding-right）
        'expansion_header_props': 'expand-icon-toggle header-style="padding: 0 14px 0 0; min-height: 44px; border-radius: 18px;"',

        'drag_icon': 'xf-icon-3d cursor-grab active:cursor-grabbing p-0.5 rounded transition-colors select-none',
        'group_name': 'font-bold truncate text-sm',

        # 关键修复 2：移除 shrink-0。因为在水平 Flex 布局中，它必须允许适度收缩，才能给右侧箭头留出位置！
        'group_header_row': 'group-sort-header w-full h-[44px] items-center justify-between pl-3 pr-2 cursor-grab active:cursor-grabbing group transition-all duration-200 select-none rounded-[18px]',

        'icon_btn': 'xf-icon-3d',
        'expansion_body': 'w-full gap-2 p-2 border-t',
        'flag_name': 'font-bold truncate',
        'bottom_wrap': 'w-full p-2 border-t mt-auto mb-4 gap-2 z-10',
        'bottom_btn': 'xf-btn-subtle w-full h-[44px] text-xs font-bold border rounded-sm px-3 py-2 transition-all',
    }


async def on_server_click_handler(server):
    logger.debug(
        f"[SidebarClick] on_server_click_handler called | server_url={server.get('url')} server_name={server.get('name')} current_view_before={scrub(CURRENT_VIEW_STATE)}")
    current_scope = CURRENT_VIEW_STATE.get('scope')
    current_data = CURRENT_VIEW_STATE.get('data')

    is_same_server = False
    if current_scope == 'SINGLE' and current_data:
        try:
            if current_data.get('url') == server.get('url'):
                is_same_server = True
        except:
            pass

    if is_same_server:
        refresher = state.REFRESH_CURRENT_NODES
        if refresher:
            res = refresher()
            if res and asyncio.iscoroutine(res):
                await res
        return

    from app.ui.pages.content_router import refresh_content

    await refresh_content('SINGLE', server)
    logger.debug(
        f"[SidebarClick] on_server_click_handler done | server_url={server.get('url')} current_view_after={scrub(CURRENT_VIEW_STATE)}")


def render_single_sidebar_row(s, register_ref=True):
    """侧边栏里的一行服务器。

    `register_ref=False` 给「离线服务器」那个固定分组用：同一台机器在侧边栏里会
    出现两次（区域分组 + 离线分组），而 SIDEBAR_UI_REFS['rows'] 是按 url 做键的
    单值——server_dialog 改完分组后拿它 row_el.move(target_col) 搬行。两份都登记
    的话后写的赢，搬走的可能是离线分组里那一份，机器就从离线分组里凭空消失了。
    所以只有归属分组里的那一行登记，副本不登记。
    """
    theme = _sidebar_theme()
    btn_name_cls = f"{theme['btn_keycap_base']} flex-grow text-xs font-bold truncate px-3 py-2.5 {theme['btn_name_text']}"
    btn_settings_cls = f"{theme['btn_keycap_base']} w-10 py-2.5 px-0 flex items-center justify-center {theme['btn_settings_text']}"

    async def open_server_settings():
        await _open_server_dialog_by_server(s, ui.context.client)

    with ui.row().classes('w-full gap-2 no-wrap items-stretch') as row:
        # 这里刻意**不用** bind_text_from(s, 'name')。
        #
        # s 是普通 dict，dict 改了没法通知谁，所以 NiceGUI 只能靠轮询发现——它每
        # 0.1 秒把全部 active_links 扫一遍。而 active_links = 服务器数 × 客户端数，
        # 且客户端断开后要等 reconnect_timeout（本项目 600 秒）才销毁，刷几次页面
        # 就攒出一堆僵尸客户端。软路由的 ARM CPU 扫 300 条要 14ms，超过 NiceGUI 的
        # MAX_PROPAGATION_TIME（10ms），于是每个周期都刷一条
        # "binding propagation for N active links took ..." 警告：CPU 白烧，日志还
        # 每秒写 10 行，在路由器的闪存上不是小事。
        #
        # 而这条绑定本来就是多余的：19 处能改服务器名的路径全都调了
        # render_sidebar_content.refresh()，refresh 会重建所有已连接客户端的侧边栏，
        # 整行连名字一起重新生成。弹窗里那些 bind_visibility_from 不受影响——源是
        # 元素属性（BindableProperty），赋值时即时传播，不靠这个轮询。
        ui.button(str(s.get('name') or '未命名'),
                  on_click=lambda _, s=s: on_server_click_handler(s)) \
            .props('no-caps align=left flat') \
            .classes(btn_name_cls) \
            .style(
            'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')

        ui.button(icon='settings', on_click=open_server_settings) \
            .props('flat square size=sm') \
            .classes(btn_settings_cls).style(
            'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-muted);').tooltip(
            '配置 / 删除')

    if register_ref:
        SIDEBAR_UI_REFS['rows'][s['url']] = row
    return row


@ui.refreshable
def render_sidebar_content():
    theme = _sidebar_theme()

    logger.debug(
        f"[Sidebar] render_sidebar_content called | servers={len(SERVERS_CACHE)} before_clear_groups={len(SIDEBAR_UI_REFS.get('groups', {}))} before_clear_rows={len(SIDEBAR_UI_REFS.get('rows', {}))}")

    SIDEBAR_UI_REFS['groups'].clear()
    SIDEBAR_UI_REFS['rows'].clear()

    with ui.column().classes(theme['top_wrap']).style(
            'border-color: var(--xf-card-border); background: linear-gradient(to bottom, var(--xf-soft-bg), var(--xf-panel-bg));'):
        ui.element('div').classes(
            'absolute inset-0 bg-[url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0IiBoZWlnaHQ9IjQiPjxyZWN0IHdpZHRoPSI0IiBoZWlnaHQ9IjQiIGZpbGw9InRyYW5zcGFyZW50Ii8+PHJlY3Qgd2lkdGg9IjEiIGhlaWdodD0iMSIgZmlsbD0icmdiYSgzNCwyMTEsMjM4LDAuMDcpIi8+PC9zdmc+")] opacity-100 pointer-events-none')
        ui.label('X-Fusion-pro').classes(theme['logo_text']).style('color: var(--xf-accent);')

        with ui.column().classes('w-full gap-2 z-10 relative'):
            ui.button('仪表盘', icon='dashboard', on_click=lambda: asyncio.create_task(_load_dashboard())).props(
                'flat align=left').classes(theme['top_btn']).style(
                'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')
            ui.button('探针监控', icon='monitor_heart', on_click=lambda: asyncio.create_task(_load_probe())).props(
                'flat align=left').classes(theme['top_btn']).style(
                'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')
            ui.button('订阅管理', icon='rss_feed', on_click=lambda: asyncio.create_task(_load_subs())).props(
                'flat align=left').classes(theme['top_btn']).style(
                'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')

    async def open_new_server_dialog():
        await _open_server_dialog(None, ui.context.client)

    async def open_all_servers(_=None):
        await _refresh_scope('ALL', client=ui.context.client)

    async def open_tag_group(tag_name):
        await _refresh_scope('TAG', tag_name, client=ui.context.client)

    async def open_country_group(group_name):
        await _refresh_scope('COUNTRY', group_name, client=ui.context.client)

    async def open_offline_group(_=None):
        await _refresh_scope('OFFLINE', client=ui.context.client)

    with ui.column().props('id=sidebar-scroll-box').classes(theme['scroll_wrap']).style(
            'background: var(--xf-bg-main);'):
        with ui.row().classes('w-full gap-2 px-1 mb-2'):
            ui.button('新建分组', icon='create_new_folder', on_click=open_quick_group_create_dialog).props(
                'flat dense').classes(f"xf-btn-primary {theme['new_group_btn']} {theme['group_action_base']}").style(
                'background: var(--xf-soft-bg); color: var(--xf-accent); border-color: var(--xf-card-border);')
            ui.button('添加服务器', icon='add', on_click=open_new_server_dialog).props('flat dense').classes(
                f"xf-btn-subtle {theme['new_server_btn']} {theme['group_action_base']}").style(
                'background: var(--xf-soft-bg); color: var(--xf-text-strong); border-color: var(--xf-card-border);')

        with ui.row().classes(theme['list_item']).style(
                'background: color-mix(in srgb, var(--xf-elevated-bg) 92%, white 8%); border-color: color-mix(in srgb, var(--xf-accent) 12%, var(--xf-card-border)); box-shadow: 0 10px 24px rgba(15,23,42,0.10), 0 1px 0 rgba(255,255,255,0.08) inset;').on(
                'click', open_all_servers):
            with ui.row().classes('items-center gap-3'):
                with ui.column().classes(theme['list_icon_box']):
                    ui.icon('dns').classes(f"{theme['list_icon']} xf-sidebar-icon-glyph").style('color: var(--xf-accent);')
                ui.label('所有服务器').classes(theme['list_label']).style('color: var(--xf-text-main);')
            ui.badge(str(len(SERVERS_CACHE)), color='blue').props('rounded-sm outline').classes(
                'text-[10px] font-black').style('color: var(--xf-accent); border-color: var(--xf-card-border);')

        # ── 固定分组：离线服务器 ──
        # 位置刻意放在「所有服务器」下面、自定义分组上面：机器掉线是最该被一眼
        # 看到的事，往下滚才能看到就晚了。
        # 这是个**筛选视图**而不是归属——机器同时还留在自己的区域 / 自定义分组里。
        offline_servers = list_offline_servers()
        try:
            offline_servers.sort(key=smart_sort_key)
        except Exception:
            offline_servers.sort(key=lambda x: x.get('name', ''))

        # 让定时自查以「屏幕上现在画的这一份」为基准，这样第一次 tick 不会无谓重建
        _LAST_OFFLINE_SNAPSHOT['urls'] = frozenset(s.get('url') for s in offline_servers)

        has_offline = bool(offline_servers)
        unmonitored_servers_list = get_unmonitored_servers()
        unmonitored = len(unmonitored_servers_list)
        offline_border = (f"color-mix(in srgb, {OFFLINE_ACCENT} 40%, var(--xf-card-border))"
                          if has_offline else 'var(--xf-card-border)')

        with ui.expansion('', icon=None, value=OFFLINE_GROUP_KEY in EXPANDED_GROUPS) \
                .classes(theme['expansion_custom']).props(theme['expansion_header_props']).style(
                f'background: color-mix(in srgb, var(--xf-elevated-bg) 94%, white 6%); '
                f'border-color: {offline_border}; '
                f'box-shadow: 0 10px 24px rgba(15,23,42,0.10), 0 1px 0 rgba(255,255,255,0.08) inset;') \
                .on_value_change(lambda e: EXPANDED_GROUPS.add(OFFLINE_GROUP_KEY) if e.value
                                 else EXPANDED_GROUPS.discard(OFFLINE_GROUP_KEY)) as off_exp:
            with off_exp.add_slot('header'):
                # 这里没有 drag_indicator：固定分组不参与排序，摆个抓手图标是骗人的。
                # 同理 group_header_row 自带的 cursor-grab 要换成 cursor-pointer。
                with ui.row().classes(f"{theme['group_header_row']} no-wrap") \
                        .classes(remove='cursor-grab active:cursor-grabbing', add='cursor-pointer') \
                        .style('color: var(--xf-text-main);') \
                        .on('click', open_offline_group):
                    with ui.row().classes('items-center gap-3 flex-grow overflow-hidden no-wrap'):
                        with ui.column().classes(theme['list_icon_box']).style(
                                f"border-color: {offline_border};"):
                            ui.icon('cloud_off' if has_offline else 'cloud_done') \
                                .classes(f"{theme['list_icon']} xf-sidebar-icon-glyph") \
                                .style(f"color: {OFFLINE_ACCENT if has_offline else 'var(--xf-text-muted)'};")
                        ui.label(OFFLINE_GROUP_LABEL).classes(theme['group_name']).style(
                            f"color: {OFFLINE_ACCENT if has_offline else 'var(--xf-text-main)'};")

                    with ui.row().classes('no-drag items-center gap-1 pr-2 flex-shrink-0').on(
                            'mousedown.stop').on('click.stop'):
                        ui.badge(str(len(offline_servers)), color='red' if has_offline else 'grey') \
                            .props('rounded-sm outline').classes('text-[10px] font-black') \
                            .style('border-color: var(--xf-card-border);') \
                            .tooltip(f"探针超过 {probe_offline_after() // 60} 分钟没推送就算离线")

            with ui.column().classes(theme['expansion_body']).style(
                    'background: color-mix(in srgb, var(--xf-elevated-bg) 84%, var(--xf-bg-main)); '
                    'border-color: color-mix(in srgb, var(--xf-accent) 8%, var(--xf-card-border));') as off_col:
                SIDEBAR_UI_REFS['groups'][OFFLINE_GROUP_KEY] = off_col
                if has_offline:
                    for s in offline_servers:
                        # register_ref=False：这是副本，别顶掉归属分组里那一行的引用
                        render_single_sidebar_row(s, register_ref=False)
                else:
                    ui.label('受监控的服务器全部在线').classes('text-xs font-bold px-1 py-0.5') \
                        .style('color: var(--xf-text-muted);')
                if unmonitored:
                    # 说清楚判定范围：没装探针的机器我们没有观测，不能算它离线
                    unmonitored_names = '、'.join([s.get('name', '未命名') for s in unmonitored_servers_list])
                    ui.label(f'另有 {unmonitored} 台未装探针，不参与离线判定') \
                        .classes('text-[11px] font-medium px-1 cursor-help') \
                        .style('color: var(--xf-text-muted); opacity: 0.8;') \
                        .tooltip(f'未装探针机器：{unmonitored_names}')

        final_tags = ADMIN_CONFIG.get('custom_groups', [])

        if final_tags:
            ui.label('自定义分组').classes(theme['section_label']).style('color: var(--xf-accent); opacity: 0.75;')
            with ui.column().props('id=sidebar-custom-group-list').classes('w-full gap-0'):
                for tag_group in final_tags:
                    tag_servers = [
                        s for s in SERVERS_CACHE
                        if isinstance(s, dict) and (tag_group in s.get('tags', []) or s.get('group') == tag_group)
                    ]
                    try:
                        tag_servers.sort(key=smart_sort_key)
                    except:
                        tag_servers.sort(key=lambda x: x.get('name', ''))
                    is_open = tag_group in EXPANDED_GROUPS

                    with ui.element('div').props(
                            f'data-group-name={json.dumps(tag_group, ensure_ascii=False)}').classes(
                            'sidebar-sort-item w-full'):
                        with ui.expansion('', icon=None, value=is_open).classes(theme['expansion_custom']).props(
                                theme['expansion_header_props']).style(
                                'background: color-mix(in srgb, var(--xf-elevated-bg) 94%, white 6%); border-color: color-mix(in srgb, var(--xf-accent) 10%, var(--xf-card-border)); box-shadow: 0 10px 24px rgba(15,23,42,0.10), 0 1px 0 rgba(255,255,255,0.08) inset;').on_value_change(
                                lambda e, g=tag_group: EXPANDED_GROUPS.add(g) if e.value else EXPANDED_GROUPS.discard(
                                    g)) as exp:
                            with exp.add_slot('header'):
                                with ui.row().classes(f"{theme['group_header_row']} no-wrap").style(
                                        'color: var(--xf-text-main);').on('click',
                                                                            lambda _, g=tag_group: open_tag_group(g)):
                                    with ui.row().classes('items-center gap-3 flex-grow overflow-hidden no-wrap'):
                                        ui.icon('drag_indicator').classes(theme['drag_icon']).on('click.stop').tooltip(
                                            '按住拖拽排序')

                                        with ui.row().classes('items-center gap-2 flex-grow overflow-hidden no-wrap'):
                                            ui.label(tag_group).classes(theme['group_name']).style('color: var(--xf-text-main);')

                                    with ui.row().classes('no-drag items-center gap-1 pr-2 flex-shrink-0').on(
                                            'mousedown.stop').on('click.stop'):
                                        ui.button(icon='settings',
                                                  on_click=lambda _, g=tag_group: open_combined_group_management(g)).props(
                                            'flat dense round size=xs padding=4px').classes(theme['icon_btn']).tooltip(
                                            '管理分组')
                                        ui.badge(str(len(tag_servers)), color='green').props(
                                            'rounded-sm outline text-color=green-4').classes(
                                            'text-[10px] font-black').style('border-color: var(--xf-card-border);')

                            with ui.column().classes(theme['expansion_body']).style(
                                    'background: color-mix(in srgb, var(--xf-elevated-bg) 84%, var(--xf-bg-main)); border-color: color-mix(in srgb, var(--xf-accent) 8%, var(--xf-card-border));') as col:
                                SIDEBAR_UI_REFS['groups'][tag_group] = col
                                for s in tag_servers:
                                    render_single_sidebar_row(s)

        ui.label('区域分组').classes(theme['section_label']).style('color: var(--xf-accent); opacity: 0.75;')
        country_buckets = {}
        for s in SERVERS_CACHE:
            c_group = detect_country_group(s.get('name', ''), s)
            if c_group in ['默认分组', '自动注册', '自动导入', '未分组', '', None]:
                c_group = '🏳️ 其他地区'
            if c_group not in country_buckets:
                country_buckets[c_group] = []
            country_buckets[c_group].append(s)

        saved_order = ADMIN_CONFIG.get('group_order', [])

        def region_sort_key(name):
            return saved_order.index(name) if name in saved_order else 9999

        sorted_regions = sorted(country_buckets.keys(), key=region_sort_key)

        with ui.column().props('id=sidebar-region-group-list').classes('w-full gap-2 pb-4'):
            for c_name in sorted_regions:
                c_servers = country_buckets[c_name]
                try:
                    c_servers.sort(key=smart_sort_key)
                except:
                    c_servers.sort(key=lambda x: x.get('name', ''))
                is_open = c_name in EXPANDED_GROUPS

                with ui.element('div').props(f'data-group-name={json.dumps(c_name, ensure_ascii=False)}').classes('sidebar-sort-item w-full'):
                    with ui.expansion('', icon=None, value=is_open).classes(theme['expansion_region']).props(
                            theme['expansion_header_props']).style(
                            'background: color-mix(in srgb, var(--xf-elevated-bg) 94%, white 6%); border-color: color-mix(in srgb, var(--xf-accent) 10%, var(--xf-card-border)); box-shadow: 0 10px 24px rgba(15,23,42,0.10), 0 1px 0 rgba(255,255,255,0.08) inset;').on_value_change(

                            lambda e, g=c_name: EXPANDED_GROUPS.add(g) if e.value else EXPANDED_GROUPS.discard(
                                    g)) as exp:
                        with exp.add_slot('header'):
                            with ui.row().classes(f"{theme['group_header_row']} no-wrap").style(
                                    'color: var(--xf-text-main);').on('click',

                                                                        lambda _, g=c_name: open_country_group(g)):
                                with ui.row().classes('items-center gap-3 flex-grow overflow-hidden'):
                                    ui.icon('drag_indicator').classes(theme['drag_icon']).on('click.stop').tooltip(
                                        '按住拖拽排序')
                                    with ui.row().classes('items-center gap-2 flex-grow'):
                                        flag = c_name.split(' ')[0] if ' ' in c_name else '🏳️'
                                        ui.label(flag).classes('text-lg filter drop-shadow-md').style(
                                            'color: var(--xf-text-main);')
                                        display_name = c_name.split(' ')[1] if ' ' in c_name else c_name
                                        ui.label(display_name).classes(theme['flag_name']).style(
                                            'color: var(--xf-text-main);')
                                with ui.row().classes('no-drag items-center gap-1 pr-2').on('mousedown.stop').on('click.stop'):
                                    ui.button(icon='edit_note',
                                              on_click=lambda _, s=c_servers, t=c_name: open_bulk_edit_dialog(s,
                                                                                                              f"区域: {t}")).props(
                                        'flat dense round size=xs padding=4px').classes(theme['icon_btn']).tooltip(
                                        '批量管理')
                                    ui.badge(str(len(c_servers)), color='green').props(
                                        'rounded-sm outline text-color=green-4').classes(
                                        'text-[10px] font-black').style('border-color: var(--xf-card-border);')

                        with ui.column().classes(theme['expansion_body']).style(
                                'background: color-mix(in srgb, var(--xf-elevated-bg) 84%, var(--xf-bg-main)); border-color: color-mix(in srgb, var(--xf-accent) 8%, var(--xf-card-border));') as col:

                            SIDEBAR_UI_REFS['groups'][c_name] = col
                            for s in c_servers:
                                render_single_sidebar_row(s)

    ui.run_javascript('''
        (function() {
            function saveSidebarOrder(kind, order) {
                return fetch('/api/sidebar/reorder', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({kind, order}),
                }).then(r => r.json()).catch(() => ({ok: false}));
            }

            function getOrder(container) {
                return Array.from(container.querySelectorAll('.sidebar-sort-item'))
                    .map(el => el.dataset.groupName)
                    .filter(Boolean);
            }

            function ensureStyle() {
                if (document.getElementById('xf-sidebar-sort-style')) return;
                const style = document.createElement('style');
                style.id = 'xf-sidebar-sort-style';
                style.textContent = `
                    .xf-sidebar-drag-ghost { opacity: 0.22 !important; }
                    .xf-sidebar-drag-chosen { transform: scale(1.01); }
                    .xf-sidebar-drag-active {
                        opacity: 0.96 !important;
                        transform: scale(1.015);
                        filter: drop-shadow(0 14px 24px rgba(15, 23, 42, 0.28));
                        z-index: 9999 !important;
                    }
                    .group-sort-header {
                        touch-action: none;
                        user-select: none;
                        -webkit-user-select: none;
                    }
                `;
                document.head.appendChild(style);
            }

            function ensureSortableScript() {
                if (window.Sortable) return Promise.resolve(window.Sortable);
                if (!window.__xfSidebarSortableLoading) {
                    window.__xfSidebarSortableLoading = new Promise((resolve, reject) => {
                        const existing = document.getElementById('xf-sidebar-sortable-script');
                        if (existing) {
                            existing.addEventListener('load', () => resolve(window.Sortable));
                            existing.addEventListener('error', reject);
                            return;
                        }
                        const script = document.createElement('script');
                        script.id = 'xf-sidebar-sortable-script';
                        script.src = 'https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js';
                        script.onload = () => resolve(window.Sortable);
                        script.onerror = reject;
                        document.head.appendChild(script);
                    });
                }
                return window.__xfSidebarSortableLoading;
            }

            function bootSortable(listId, kind, retries) {
                const container = document.getElementById(listId);
                const items = container ? container.querySelectorAll('.sidebar-sort-item') : [];
                if (!container || !items.length) {
                    if (retries > 0) {
                        setTimeout(() => bootSortable(listId, kind, retries - 1), 180);
                    } else {
                        console.warn('[SidebarSort] 容器或分组项未就绪', listId, {hasContainer: !!container, itemCount: items.length});
                    }
                    return;
                }

                ensureSortableScript().then(() => {
                    if (!window.Sortable) {
                        console.warn('[SidebarSort] Sortable 仍未可用', listId);
                        return;
                    }
                    window.xfSidebarSortables = window.xfSidebarSortables || {};
                    if (window.xfSidebarSortables[listId]) {
                        window.xfSidebarSortables[listId].destroy();
                    }
                    window.xfSidebarSortables[listId] = new window.Sortable(container, {
                        animation: 220,
                        easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
                        handle: '.group-sort-header',
                        draggable: '.sidebar-sort-item',
                        filter: '.no-drag, .no-drag *, .q-btn, .q-btn *, .q-expansion-item__toggle-icon, .q-expansion-item__toggle-icon *',
                        preventOnFilter: false,
                        ghostClass: 'xf-sidebar-drag-ghost',
                        chosenClass: 'xf-sidebar-drag-chosen',
                        dragClass: 'xf-sidebar-drag-active',
                        forceFallback: true,
                        fallbackOnBody: true,
                        swapThreshold: 0.65,
                        fallbackTolerance: 3,
                        scroll: true,
                        bubbleScroll: true,
                        scrollSensitivity: 70,
                        scrollSpeed: 18,
                        delay: 0,
                        onStart: function () {
                            window.__xfSidebarDragJustEndedAt = 0;
                            document.body.classList.add('xf-sidebar-sorting');
                        },
                        onEnd: async function () {
                            document.body.classList.remove('xf-sidebar-sorting');
                            window.__xfSidebarDragJustEndedAt = Date.now();
                            const order = getOrder(container);
                            const res = await saveSidebarOrder(kind, order);
                            if (!res || !res.ok) {
                                console.warn('[SidebarSort] 保存排序失败', kind, order, res);
                            } else {
                                console.info('[SidebarSort] 排序已保存', kind, order);
                            }
                        },
                    });
                    console.info('[SidebarSort] 初始化成功', listId, {kind, itemCount: items.length});
                }).catch(err => {
                    console.error('[SidebarSort] SortableJS 加载失败', err);
                });
            }

            ensureStyle();

            if (!window.__xfSidebarDragClickGuardBound) {
                document.addEventListener('click', function(e) {
                    const header = e.target && e.target.closest ? e.target.closest('.group-sort-header') : null;
                    if (!header) return;
                    const justEndedAt = window.__xfSidebarDragJustEndedAt || 0;
                    if (Date.now() - justEndedAt < 280) {
                        e.preventDefault();
                        e.stopPropagation();
                        e.stopImmediatePropagation && e.stopImmediatePropagation();
                    }
                }, true);
                window.__xfSidebarDragClickGuardBound = true;
            }

            var el = document.getElementById('sidebar-scroll-box');
            if (el) {
                if (window.sidebarScroll) el.scrollTop = window.sidebarScroll;
                if (!el.dataset.scrollBound) {
                    el.addEventListener('scroll', function() { window.sidebarScroll = el.scrollTop; });
                    el.dataset.scrollBound = '1';
                }
            }

            setTimeout(() => {
                bootSortable('sidebar-custom-group-list', 'custom', 8);
                bootSortable('sidebar-region-group-list', 'region', 8);
            }, 120);
        })();
    ''')

    logger.debug(
        f"[Sidebar] render_sidebar_content finished | servers={len(SERVERS_CACHE)} groups={len(SIDEBAR_UI_REFS.get('groups', {}))} rows={len(SIDEBAR_UI_REFS.get('rows', {}))}")

    with ui.column().classes(theme['bottom_wrap']).style(
            'border-color: var(--xf-card-border); background: var(--xf-bg-main);'):
        ui.button('批量 SSH 执行', icon='playlist_play', on_click=batch_ssh_manager.open_dialog).props(
            'flat align=left').classes(theme['bottom_btn']).style(
            'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')
        ui.button('连接与通知设置', icon='tune', on_click=open_probe_settings_dialog).props(
            'flat align=left').classes(theme['bottom_btn']).style(
            'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')
        ui.button('Cloudflare 设置', icon='cloud', on_click=open_cloudflare_settings_dialog).props(
            'flat align=left').classes(theme['bottom_btn']).style(
            'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')
        ui.button('全局 SSH 设置', icon='vpn_key', on_click=open_global_settings_dialog).props(
            'flat align=left').classes(theme['bottom_btn']).style(
            'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')
        ui.button('数据备份 / 恢复', icon='save', on_click=open_data_mgmt_dialog).props('flat align=left').classes(
            theme['bottom_btn']).style(
            'background: var(--xf-elevated-bg); border-color: var(--xf-card-border); color: var(--xf-text-strong);')


async def _load_dashboard():
    from app.ui.components.dashboard import load_dashboard_stats

    await load_dashboard_stats()


async def _load_subs():
    from app.ui.pages.subs_page import load_subs_view

    await load_subs_view()


async def _load_probe():
    from app.ui.pages.probe_page import load_probe_page

    await load_probe_page()


async def _refresh_scope(scope, data=None, client=None):
    from app.ui.pages.content_router import refresh_content

    logger.debug(
        f"[SidebarClick] _refresh_scope called | scope={scope} data={scrub(data)} client_present={client is not None} current_view_before={scrub(CURRENT_VIEW_STATE)}")
    await refresh_content(scope, data, manual_client=client)
    logger.debug(
        f"[SidebarClick] _refresh_scope done | scope={scope} data={scrub(data)} client_present={client is not None} current_view_after={scrub(CURRENT_VIEW_STATE)}")


async def _open_server_dialog(index, client=None):
    from app.ui.dialogs.server_dialog import open_server_dialog

    if client:
        with client:
            await open_server_dialog(index)
        return

    await open_server_dialog(index)


async def _open_server_dialog_by_server(server, client=None):
    from app.ui.dialogs.server_dialog import open_server_dialog

    if client:
        with client:
            await open_server_dialog(SERVERS_CACHE.index(server))
        return

    await open_server_dialog(SERVERS_CACHE.index(server))