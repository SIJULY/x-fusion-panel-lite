"""探针监控页。

展示所有受监控服务器的实时状态概览——CPU、内存、磁盘、网络、运行时间等，
类似于哪吒探针 / ServerStatus 的面板效果。

数据来源是内存中的 PROBE_DATA_CACHE（探针 agent 定期推送），刷新间隔跟随
探针推送间隔（半分钟 ~ 半小时级），前端每 30 秒重绘一次。

布局：
  顶部 — 统计概览（总服务器 / 在线 / 离线 / 未监控）
  中部 — 每台服务器一张卡片，按在线→离线排序，展示关键指标
"""

import time

from nicegui import app, ui

from app.core.state import (
    ADMIN_CONFIG,
    CURRENT_VIEW_STATE,
    PROBE_DATA_CACHE,
    SERVERS_CACHE,
)
from app.services.probe import (
    is_server_monitored,
    is_server_offline,
    probe_offline_after,
    probe_push_interval,
)
from app.utils.formatters import format_bytes, format_push_age


def _clamp(v, lo=0.0, hi=100.0):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _progress_color(pct: float) -> str:
    if pct >= 90:
        return '#f43f5e'
    if pct >= 70:
        return '#f59e0b'
    return '#22d3ee'


def _build_server_snapshot(server_conf: dict) -> dict:
    """从 PROBE_DATA_CACHE 中构建一台服务器的探针快照。"""
    url = server_conf.get('url', '')
    probe = PROBE_DATA_CACHE.get(url) or {}
    if not isinstance(probe, dict):
        probe = {}
    static = probe.get('static') or {}
    if not isinstance(static, dict):
        static = {}

    now = time.time()
    last_push = _to_float(probe.get('last_updated', 0))
    push_age = max(0.0, now - last_push) if last_push else None
    is_stale = bool(probe and push_age is not None and push_age >= probe_offline_after())

    cpu_pct = 0.0 if is_stale else _clamp(probe.get('cpu_usage', 0.0))
    cpu_cores = int(_to_float(probe.get('cpu_cores') or static.get('cpu_cores') or 0))

    mem_total = _to_float(probe.get('mem_total', 0.0))
    mem_pct = 0.0 if is_stale else _clamp(probe.get('mem_usage', 0.0))
    mem_used = round(mem_total * mem_pct / 100.0, 2)

    swap_total = _to_float(probe.get('swap_total', 0.0))
    swap_free = _to_float(probe.get('swap_free', 0.0))
    swap_used = max(swap_total - swap_free, 0.0)
    swap_pct = 0.0 if is_stale else _clamp(
        (swap_used / swap_total * 100.0) if swap_total else 0.0)

    disk_total = _to_float(probe.get('disk_total', 0.0))
    disk_pct = _clamp(probe.get('disk_usage', 0.0))
    disk_used = round(disk_total * disk_pct / 100.0, 2)

    net_in = int(_to_float(probe.get('net_total_in', 0)))
    net_out = int(_to_float(probe.get('net_total_out', 0)))

    uptime = probe.get('uptime') or '--'
    if is_stale:
        uptime = '⚠️ 已离线'

    os_name = static.get('os') or '--'
    arch = static.get('arch') or '--'

    monitored = is_server_monitored(server_conf)
    offline = is_server_offline(server_conf)
    online = monitored and not offline

    return {
        'name': server_conf.get('name', '未命名'),
        'url': url,
        'online': online,
        'offline': offline,
        'monitored': monitored,
        'has_probe': bool(probe),
        'cpu_pct': cpu_pct,
        'cpu_cores': cpu_cores,
        'mem_total_gb': mem_total,
        'mem_used_gb': mem_used,
        'mem_pct': mem_pct,
        'swap_total_gb': swap_total,
        'swap_used_gb': swap_used,
        'swap_pct': swap_pct,
        'disk_total_gb': disk_total,
        'disk_used_gb': disk_used,
        'disk_pct': disk_pct,
        'net_in': net_in,
        'net_out': net_out,
        'uptime': uptime,
        'os': os_name,
        'arch': arch,
        'push_age': push_age,
        'data_age_text': format_push_age(push_age),
    }



async def load_probe_page():
    """加载探针监控页到 content_container。"""
    global CURRENT_VIEW_STATE
    CURRENT_VIEW_STATE['scope'] = 'PROBE'
    CURRENT_VIEW_STATE['data'] = None
    CURRENT_VIEW_STATE['page'] = 1

    try:
        app.storage.user['last_view_scope'] = 'PROBE'
        app.storage.user['last_view_data'] = None
        app.storage.user['last_view_page'] = 1
    except Exception:
        pass

    is_dark = bool(app.storage.user.get('is_dark', True))

    from app.ui.pages.content_router import content_container

    content_container.clear()
    content_container.classes(
        remove='justify-center items-center overflow-hidden p-6',
        add='overflow-y-auto p-4 pl-6 justify-start',
    )
    content_container.style('background-color: var(--xf-bg-main);')

    with content_container:
        with ui.row().classes(
                'w-full items-center justify-between mb-4 border-b pb-3'
        ).style('border-color: var(--xf-card-border);'):
            with ui.row().classes('items-center gap-3'):
                ui.icon('monitor_heart').classes('text-2xl').style(
                    'color: var(--xf-accent);')
                ui.label('探针监控').classes(
                    'text-2xl font-black tracking-wide').style(
                    'color: var(--xf-text-strong);')
            with ui.row().classes('items-center gap-2'):
                ui.label(
                    f'推送间隔: {probe_push_interval() // 60} 分钟'
                ).classes(
                    'text-xs font-bold px-2 py-1 rounded-sm border'
                ).style(
                    'color: var(--xf-text-muted); '
                    'background: var(--xf-soft-bg); '
                    'border-color: var(--xf-card-border);')
                ui.label(
                    f'离线阈值: {probe_offline_after() // 60} 分钟'
                ).classes(
                    'text-xs font-bold px-2 py-1 rounded-sm border'
                ).style(
                    'color: var(--xf-text-muted); '
                    'background: var(--xf-soft-bg); '
                    'border-color: var(--xf-card-border);')

        @ui.refreshable
        def render_probe_cards():
            _render_probe_content(is_dark)

        render_probe_cards()
        ui.timer(30.0, render_probe_cards.refresh)

def _render_probe_content(is_dark: bool, search_term: str = '', group_filter: str = 'all', group_select_ui=None):
    """渲染探针监控的核心内容：统计概览 + 服务器卡片网格。"""
    snapshots = []
    
    # 获取服务器的分组信息
    def _get_server_groups(s: dict) -> list[str]:
        tags = s.get('tags', [])
        if not tags:
            return []
        
        # 统一处理“离线”、“未监控”这类特殊分组标识，按您的逻辑
        offline = is_server_offline(s)
        monitored = is_server_monitored(s)
        
        assigned_groups = []
        if offline and monitored:
            assigned_groups.append('离线设备')
            
        custom_groups = ADMIN_CONFIG.get('custom_groups', [])
        for t in tags:
            if t in custom_groups:
                assigned_groups.append(t)
        
        if not assigned_groups:
             assigned_groups.append('未分组')
        
        return list(set(assigned_groups))
    
    all_groups_set = set()
    for s in SERVERS_CACHE:
        if not isinstance(s, dict):
            continue
        snap = _build_server_snapshot(s)
        groups = _get_server_groups(s)
        snap['groups'] = groups
        for g in groups:
            all_groups_set.add(g)
        snapshots.append(snap)
        
    if group_select_ui:
        options = {'all': '全部分组'}
        if '离线设备' in all_groups_set:
            options['离线设备'] = '离线设备'
            all_groups_set.discard('离线设备')
        if '未分组' in all_groups_set:
            options['未分组'] = '未分组'
            all_groups_set.discard('未分组')
            
        for g in sorted(list(all_groups_set)):
            options[g] = g
            
        group_select_ui.options = options
        group_select_ui.update()

    if search_term:
        term = search_term.lower()
        snapshots = [s for s in snapshots if term in s['name'].lower()]
        
    if group_filter != 'all':
        snapshots = [s for s in snapshots if group_filter in s['groups']]

    total = len(snapshots)
    online_count = sum(1 for snap in snapshots if snap['online'])
    offline_count = sum(1 for snap in snapshots if snap['offline'])
    unmonitored_count = sum(1 for snap in snapshots if not snap['monitored'])

    stat_card_cls = (
        'flex-1 min-w-[160px] p-4 rounded-sm border '
        'relative overflow-hidden transition-all duration-200'
    )

    with ui.row().classes('w-full gap-4 mb-6 flex-wrap'):
        _render_stat_card(
            '服务器总数', str(total), 'dns', 'var(--xf-accent)',
            'rgba(34,211,238,0.12)' if is_dark else 'rgba(14,165,233,0.10)',
            stat_card_cls)
        _render_stat_card(
            '在线', str(online_count), 'cloud_done', '#22c55e',
            'rgba(34,197,94,0.12)' if is_dark else 'rgba(34,197,94,0.10)',
            stat_card_cls)
        _render_stat_card(
            '离线', str(offline_count), 'cloud_off', '#f43f5e',
            'rgba(244,63,94,0.12)' if is_dark else 'rgba(244,63,94,0.10)',
            stat_card_cls)
        _render_stat_card(
            '未监控', str(unmonitored_count), 'visibility_off', '#94a3b8',
            'rgba(148,163,184,0.10)' if is_dark else 'rgba(100,116,139,0.08)',
            stat_card_cls)

    if not snapshots:
        with ui.column().classes('w-full h-64 justify-center items-center'):
            ui.icon('inbox', size='4rem').style('color: var(--xf-text-muted);')
            ui.label('暂无服务器').classes('text-sm font-bold').style(
                'color: var(--xf-text-muted);')
        return

    def sort_key(snap):
        priority = 0 if snap['online'] else (1 if snap['offline'] else 2)
        return (priority, snap['name'].lower())

    snapshots.sort(key=sort_key)

    with ui.element('div').classes('w-full').style(
            'display: grid; '
            'grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); '
            'gap: 16px;'):
        for snap in snapshots:
            _render_server_card(snap, is_dark)



def _render_stat_card(title, value, icon, color, bg, cls):
    with ui.card().classes(cls).style(
            f'background: {bg}; border-color: var(--xf-card-border); '
            f'box-shadow: 0 6px 18px rgba(15,23,42,0.08);'):
        with ui.row().classes('items-center justify-between w-full'):
            with ui.column().classes('gap-0'):
                ui.label(title).classes(
                    'text-[11px] font-black uppercase tracking-wider '
                    'opacity-80').style('color: var(--xf-text-muted);')
                ui.label(value).classes(
                    'text-3xl font-black tracking-tight').style(
                    f'color: {color};')
            with ui.element('div').classes(
                    'w-12 h-12 rounded-xl flex items-center '
                    'justify-center border').style(
                    f'background: {bg}; '
                    f'border-color: color-mix(in srgb, {color} 30%, '
                    f'transparent);'):
                ui.icon(icon).classes('text-3xl').style(
                    f'color: {color}; opacity: 0.85;')



def _render_server_card(snap: dict, is_dark: bool):
    """渲染一台服务器的探针状态卡片。"""
    if snap['online']:
        status_color = '#22c55e'
        status_text = 'ONLINE'
        status_icon = 'circle'
        border_accent = 'color-mix(in srgb, #22c55e 25%, var(--xf-card-border))'
    elif snap['offline']:
        status_color = '#f43f5e'
        status_text = 'OFFLINE'
        status_icon = 'circle'
        border_accent = 'color-mix(in srgb, #f43f5e 25%, var(--xf-card-border))'
    else:
        status_color = '#94a3b8'
        status_text = '未监控'
        status_icon = 'radio_button_unchecked'
        border_accent = 'var(--xf-card-border)'

    with ui.card().classes(
            'p-0 gap-0 rounded-sm border overflow-hidden '
            'transition-all duration-200 hover:-translate-y-[1px]'
    ).style(
            f'background: var(--xf-panel-bg); '
            f'border-color: {border_accent}; '
            f'box-shadow: 0 6px 18px rgba(15,23,42,0.10);'):
        _render_card_header(snap, status_color, status_text,
                            status_icon, border_accent)
        if not snap['monitored']:
            _render_card_unmonitored()
            return
        _render_card_sysinfo(snap)
        _render_card_metrics(snap)
        _render_card_network(snap)



def _render_card_header(snap, status_color, status_text,
                        status_icon, border_accent):
    with ui.row().classes(
            'w-full items-center justify-between px-4 py-3 border-b'
    ).style(f'border-color: {border_accent};'):
        with ui.row().classes('items-center gap-2 overflow-hidden'):
            ui.icon('dns').classes('text-base flex-shrink-0').style(
                'color: var(--xf-accent);')
            ui.label(snap['name']).classes(
                'text-sm font-black truncate').style(
                'color: var(--xf-text-strong);')
        with ui.row().classes('items-center gap-1.5 flex-shrink-0'):
            ui.icon(status_icon).classes('text-[8px]').style(
                f'color: {status_color};')
            ui.label(status_text).classes(
                'text-[10px] font-black tracking-wider').style(
                f'color: {status_color};')


def _render_card_unmonitored():
    with ui.column().classes(
            'w-full px-4 py-6 items-center justify-center gap-2'):
        ui.icon('visibility_off', size='2rem').style(
            'color: var(--xf-text-muted); opacity: 0.5;')
        ui.label('未安装探针').classes('text-xs font-bold').style(
            'color: var(--xf-text-muted);')



def _render_card_sysinfo(snap):
    with ui.row().classes(
            'w-full px-4 pt-3 pb-1 items-center gap-4 flex-wrap'):
        if snap['os'] and snap['os'] != '--':
            clean_os = (snap['os'].split('(')[0]
                        .replace('GNU/Linux', '')
                        .replace('  ', ' ').strip())
            if clean_os:
                with ui.row().classes('items-center gap-1'):
                    ui.icon('computer').classes('text-xs').style(
                        'color: var(--xf-text-muted);')
                    ui.label(clean_os).classes(
                        'text-[11px] font-bold').style(
                        'color: var(--xf-text-muted);')
        if snap['arch'] and snap['arch'] != '--':
            with ui.row().classes('items-center gap-1'):
                ui.icon('memory').classes('text-xs').style(
                    'color: var(--xf-text-muted);')
                ui.label(snap['arch']).classes(
                    'text-[11px] font-bold').style(
                    'color: var(--xf-text-muted);')
        if snap['data_age_text']:
            with ui.row().classes('items-center gap-1'):
                ui.icon('schedule').classes('text-xs').style(
                    'color: var(--xf-text-muted);')
                ui.label(snap['data_age_text']).classes(
                    'text-[11px] font-bold').style(
                    'color: var(--xf-text-muted);')



def _render_card_metrics(snap):
    with ui.element('div').classes('w-full px-4 py-3').style(
            'display: grid; grid-template-columns: 1fr 1fr; '
            'gap: 10px;'):
        _render_metric_bar(
            'CPU', f'{snap["cpu_pct"]:.1f}%', snap['cpu_pct'],
            sub=f'{snap["cpu_cores"]} 核' if snap['cpu_cores'] else None,
            icon='speed')
        _render_metric_bar(
            '内存', f'{snap["mem_pct"]:.1f}%', snap['mem_pct'],
            sub=(f'{snap["mem_used_gb"]:.1f} / '
                 f'{snap["mem_total_gb"]:.1f} GB'),
            icon='memory')
        _render_metric_bar(
            '磁盘', f'{snap["disk_pct"]:.1f}%', snap['disk_pct'],
            sub=(f'{snap["disk_used_gb"]:.1f} / '
                 f'{snap["disk_total_gb"]:.1f} GB'),
            icon='storage')
        uptime_display = snap['uptime']
        if isinstance(uptime_display, str) and len(uptime_display) > 20:
            uptime_display = uptime_display[:20] + '…'
        with ui.column().classes('gap-1 p-2 rounded-sm border').style(
                'background: var(--xf-soft-bg); '
                'border-color: var(--xf-card-border);'):
            with ui.row().classes('items-center gap-1'):
                ui.icon('timer').classes('text-xs').style(
                    'color: var(--xf-accent);')
                ui.label('运行时间').classes(
                    'text-[10px] font-black uppercase '
                    'tracking-wider').style(
                    'color: var(--xf-text-muted);')
            ui.label(uptime_display).classes(
                'text-xs font-black truncate w-full').style(
                'color: var(--xf-text-strong);')



def _render_card_network(snap):
    with ui.row().classes(
            'w-full px-4 pb-3 items-center justify-between'):
        with ui.row().classes('items-center gap-3'):
            with ui.row().classes('items-center gap-1'):
                ui.icon('arrow_downward').classes('text-xs').style(
                    'color: #22c55e;')
                ui.label(
                    f'↓ {format_bytes(snap["net_in"])}'
                ).classes(
                    'text-[11px] font-bold font-mono').style(
                    'color: var(--xf-text-muted);')
            with ui.row().classes('items-center gap-1'):
                ui.icon('arrow_upward').classes('text-xs').style(
                    'color: #3b82f6;')
                ui.label(
                    f'↑ {format_bytes(snap["net_out"])}'
                ).classes(
                    'text-[11px] font-bold font-mono').style(
                    'color: var(--xf-text-muted);')
        if snap['swap_total_gb'] > 0:
            with ui.row().classes('items-center gap-1'):
                ui.label(
                    f'SWAP {snap["swap_used_gb"]:.1f}/'
                    f'{snap["swap_total_gb"]:.1f} GB'
                ).classes('text-[10px] font-bold').style(
                    'color: var(--xf-text-muted);')


def _render_metric_bar(label, value_text, pct,
                       sub=None, icon='circle'):
    """渲染一个带进度条的指标小格子。"""
    color = _progress_color(pct)
    with ui.column().classes('gap-1 p-2 rounded-sm border').style(
            'background: var(--xf-soft-bg); '
            'border-color: var(--xf-card-border);'):
        with ui.row().classes(
                'items-center justify-between w-full'):
            with ui.row().classes('items-center gap-1'):
                ui.icon(icon).classes('text-xs').style(
                    f'color: {color};')
                ui.label(label).classes(
                    'text-[10px] font-black uppercase '
                    'tracking-wider').style(
                    'color: var(--xf-text-muted);')
            ui.label(value_text).classes(
                'text-xs font-black').style(f'color: {color};')
        with ui.element('div').classes(
                'w-full h-1.5 rounded-full overflow-hidden'
        ).style('background: var(--xf-card-border);'):
            ui.element('div').classes(
                'h-full rounded-full transition-all duration-500'
            ).style(
                f'width: {min(pct, 100):.1f}%; background: {color};')
        if sub:
            ui.label(sub).classes('text-[10px] font-bold').style(
                'color: var(--xf-text-muted);')

