"""探针监控页。

展示所有受监控服务器的实时状态概览——CPU、负载、内存、磁盘、网络速率等，
类似于哪吒探针 / ServerStatus 的面板效果。

数据来源是内存中的 PROBE_DATA_CACHE（探针 agent 定期推送）。前端每 30 秒醒
一次，但只在真实数据或在线/离线状态变了时才重绘。不要为了「N 分钟前」这类
纯时间文案重建整个卡片网格，否则所有系统图标会同步重新加载，看起来像集体闪烁。

布局：
  顶部 — 搜索框 + 分组筛选，然后是统计概览（总服务器 / 在线 / 离线 / 未监控）
  中部 — 每台服务器一张卡片，按离线→在线→未监控排序，展示关键指标

配色一律走 --xf-* 主题变量，或 color-mix(语义色, var(--xf-panel-bg))，不要按
is_dark 把 rgba 写死：主题切换是 JS 热替换 CSS 变量（见 main_page 的
applyXFusionDomTheme），Python 端不会重新渲染，写死的颜色切主题后不会跟着变。
"""

import time
from functools import partial

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
from app.ui.common.notifications import safe_notify
from app.utils.formatters import format_bytes, format_push_age

STATUS_ONLINE = '#22c55e'
STATUS_OFFLINE = '#f43f5e'
STATUS_IDLE = '#94a3b8'


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
        return STATUS_OFFLINE
    if pct >= 70:
        return '#f59e0b'
    return '#22d3ee'


def _tint(color: str, pct: int) -> str:
    """把语义色按比例混到面板底色上。

    用 color-mix 而不是按 is_dark 写死两套 rgba：--xf-panel-bg 是活的 CSS
    变量，切换明暗主题时浏览器自己会重算，Python 端不必重新渲染。
    """
    return f'color-mix(in srgb, {color} {pct}%, var(--xf-panel-bg))'


def _format_speed(bps) -> str:
    """字节/秒 → 人类可读速率；None 表示没有数据。"""
    if bps is None:
        return '--'
    return f'{format_bytes(bps)}/s'


def _usage_text(used, total) -> str:
    """「已用 / 总量 GB」。已用未知（机器离线）时左边留 --，总量仍然有意义。"""
    if not total:
        return ''
    used_text = '--' if used is None else f'{used:.1f}'
    return f'{used_text} / {total:.1f} GB'


def _format_cpu_pct(pct) -> str:
    """CPU 百分比显示文案。

    agent 侧 CPU 采样只有 1 秒且会 round 到 1 位小数，极低负载时经常被压成
    0.0。在线机器直接显示 0% 很像数据坏了；低于 1% 统一显示成 <1%，既避免
    满屏 0%，也不谎报成一个确定的 1%。
    """
    if pct is None:
        return '--'
    if pct < 1:
        return '<1%'
    return f'{pct:.1f}%'


# 「离线设备」是虚拟分组，不存在于 custom_groups 里，只在探针页用来快速过滤
OFFLINE_GROUP_LABEL = '🔴 离线设备'
UNGROUPED_LABEL = '未分组'


def _server_group_names(server_conf: dict) -> list[str]:
    """一台服务器所属的分组名列表。

    判定口径跟侧边栏 / 订阅保持一致：主分组存在 server_conf['group']，tags 是
    额外挂上去的自定义分组，两者都算（参见 sidebar.render_sidebar_content）。
    被监控且已掉线的机器再额外归入「离线设备」这个虚拟分组。
    """
    names: list[str] = []

    primary = str(server_conf.get('group') or '').strip()
    if primary:
        names.append(primary)

    custom_groups = ADMIN_CONFIG.get('custom_groups') or []
    for tag in (server_conf.get('tags') or []):
        if tag in custom_groups and tag not in names:
            names.append(tag)

    if not names:
        names.append(UNGROUPED_LABEL)

    if is_server_monitored(server_conf) and is_server_offline(server_conf):
        names.append(OFFLINE_GROUP_LABEL)

    return names


def _probe_group_options() -> dict[str, str]:
    """分组下拉框的选项：全部分组 → 离线设备 → 未分组 → 其余分组按名称排序。"""
    found = set()
    for server_conf in SERVERS_CACHE:
        if not isinstance(server_conf, dict):
            continue
        found.update(_server_group_names(server_conf))

    options = {'all': '全部分组'}
    for pinned in (OFFLINE_GROUP_LABEL, UNGROUPED_LABEL):
        if pinned in found:
            options[pinned] = pinned
            found.discard(pinned)
    for name in sorted(found):
        options[name] = name
    return options


def _probe_fingerprint() -> tuple:
    """页面内容的指纹：变了才值得重绘。

    各机器的推送时间戳和在线/离线判定会改变卡片内容，需要触发重绘；但不要把
    推送年龄文案档位放进指纹，否则没有新数据时也会整页重建，导致远程 OS 图标
    在所有卡片上同步闪烁。
    """
    parts = []
    for server_conf in SERVERS_CACHE:
        if not isinstance(server_conf, dict):
            continue
        url = server_conf.get('url', '')
        probe = PROBE_DATA_CACHE.get(url)
        last_push = (_to_float(probe.get('last_updated', 0))
                     if isinstance(probe, dict) else 0.0)
        parts.append((
            url,
            server_conf.get('name'),
            server_conf.get('group'),
            tuple(server_conf.get('tags') or ()),
            last_push,
            is_server_monitored(server_conf),
            is_server_offline(server_conf),
        ))
    return tuple(parts)


def _build_server_snapshot(server_conf: dict) -> dict:
    """从 PROBE_DATA_CACHE 中构建一台服务器的探针快照。

    没有有效数据时，各项瞬时指标一律给 None 而不是 0——卡片上的 0.0% 读起来
    像「这机器很闲」，实际含义是「没有数据」，两件事不能混。

    「有没有有效数据」直接问 is_server_offline，不在这里另算一遍过期：那个函数
    已经把两种无数据情形都盖住了——推送过但超了阈值、以及标了 probe_installed
    却一次都没推过（装完没跑起来）。后者的 cache 是空的，如果按 push_age 自己
    判，空 cache 反而判不出「过期」，于是又会掉回显示 0.0%。

    累计流量是单调计数器，最后一次已知值仍然有意义，所以照旧显示。
    """
    url = server_conf.get('url', '')
    probe = PROBE_DATA_CACHE.get(url) or {}
    if not isinstance(probe, dict):
        probe = {}
    static = probe.get('static') or {}
    if not isinstance(static, dict):
        static = {}

    monitored = is_server_monitored(server_conf)
    offline = is_server_offline(server_conf)

    now = time.time()
    last_push = _to_float(probe.get('last_updated', 0))
    push_age = max(0.0, now - last_push) if last_push else None
    unknown = offline or not probe
    last_uptime = str(probe.get('uptime') or '').strip() or None

    cpu_pct = None if unknown else _clamp(probe.get('cpu_usage', 0.0))
    cpu_cores = int(_to_float(probe.get('cpu_cores')
                              or static.get('cpu_cores') or 0))
    # load_1 是 agent 一直在推的字段，老版本 agent 可能没有，取不到就当没数据
    load_1 = None if unknown else _to_float(probe.get('load_1'), None)

    mem_total = _to_float(probe.get('mem_total', 0.0))
    mem_pct = None if unknown else _clamp(probe.get('mem_usage', 0.0))
    mem_used = None if mem_pct is None else round(mem_total * mem_pct / 100.0, 2)

    swap_total = _to_float(probe.get('swap_total', 0.0))
    if unknown or swap_total <= 0:
        swap_used = None
        swap_pct = None
    else:
        swap_used = max(swap_total - _to_float(probe.get('swap_free', 0.0)), 0.0)
        swap_pct = _clamp(swap_used / swap_total * 100.0)

    disk_total = _to_float(probe.get('disk_total', 0.0))
    disk_pct = None if unknown else _clamp(probe.get('disk_usage', 0.0))
    disk_used = None if disk_pct is None else round(
        disk_total * disk_pct / 100.0, 2)

    net_in = int(_to_float(probe.get('net_total_in', 0)))
    net_out = int(_to_float(probe.get('net_total_out', 0)))
    speed_in = None if unknown else _to_float(probe.get('net_speed_in'), None)
    speed_out = None if unknown else _to_float(probe.get('net_speed_out'), None)

    # agent 检测不到虚拟化时会填 "Unknown"，那就当没这条信息，别占版面
    virt = str(static.get('virt') or '').strip()
    if virt.lower() in ('', 'unknown', 'none'):
        virt = ''

    return {
        'name': server_conf.get('name', '未命名'),
        'url': url,
        'online': monitored and not offline,
        'offline': offline,
        'monitored': monitored,
        'has_probe': bool(probe),
        'cpu_pct': cpu_pct,
        'cpu_cores': cpu_cores,
        'cpu_model': str(static.get('cpu_model') or '').strip(),
        'load_1': load_1,
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
        'speed_in': speed_in,
        'speed_out': speed_out,
        # uptime 是最后一次探针上报时的系统运行时长。机器离线后没有新值，继续
        # 展示这条最后已知值，避免被 unknown 分支抹成 0/--，看起来像 VPS 重启。
        'uptime': last_uptime,
        'os': str(static.get('os') or '').strip(),
        'arch': str(static.get('arch') or '').strip(),
        'virt': virt,
        'last_push': last_push,
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

    from app.ui.pages.content_router import content_container

    content_container.clear()
    content_container.classes(
        remove='justify-center items-center overflow-hidden p-6',
        add='overflow-y-auto p-4 pl-6 justify-start',
    )
    content_container.style('background-color: var(--xf-bg-main);')

    with content_container:
        @ui.refreshable
        def render_probe_cards():
            _render_probe_content(
                search_term=search_input.value,
                group_filter=group_select.value,
            )

        # 上一次重绘时的内容指纹：定时器 30 秒醒一次，但没变化就不动 DOM
        last_fingerprint = {'value': None}

        def refresh_all():
            fingerprint = _probe_fingerprint()
            if fingerprint == last_fingerprint['value']:
                return
            last_fingerprint['value'] = fingerprint

            # 服务器上下线 / 改分组后，下拉框的选项也要跟着变
            options = _probe_group_options()
            if options != group_select.options:
                group_select.set_options(
                    options,
                    # 选中的分组可能已经消失（机器被删或改了组），退回「全部分组」
                    value=group_select.value if group_select.value in options else 'all',
                )
            render_probe_cards.refresh()

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
                    f'最大间隔: {probe_push_interval() // 60} 分钟'
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

        with ui.row().classes('w-full items-center gap-3 mb-4 flex-wrap'):
            search_input = ui.input(
                placeholder='搜索服务器名称或 IP',
                on_change=lambda _: render_probe_cards.refresh(),
            ).props('dense outlined clearable debounce="300"').classes(
                'w-[280px] max-w-full')

            group_select = ui.select(
                _probe_group_options(),
                value='all',
                on_change=lambda _: render_probe_cards.refresh(),
            ).props('dense outlined').classes('w-[200px] max-w-full')

        # 先播种指纹，免得挂上定时器后第一次 tick 就白重绘一遍
        last_fingerprint['value'] = _probe_fingerprint()
        render_probe_cards()
        ui.timer(30.0, refresh_all)


def _render_probe_content(search_term: str = '', group_filter: str = 'all'):
    """渲染探针监控的核心内容：统计概览 + 服务器卡片网格。"""
    from app.ui.pages.content_router import _match_server_search

    keyword = str(search_term or '').strip()
    group_filter = group_filter or 'all'

    snapshots = []
    for server_conf in SERVERS_CACHE:
        if not isinstance(server_conf, dict):
            continue
        if keyword and not _match_server_search(server_conf, keyword):
            continue
        groups = _server_group_names(server_conf)
        if group_filter != 'all' and group_filter not in groups:
            continue
        snap = _build_server_snapshot(server_conf)
        snap['groups'] = groups
        snapshots.append(snap)

    total = len(snapshots)
    online_count = sum(1 for snap in snapshots if snap['online'])
    offline_count = sum(1 for snap in snapshots if snap['offline'])
    unmonitored_count = sum(1 for snap in snapshots if not snap['monitored'])

    stat_card_cls = (
        'flex-1 min-w-[160px] p-4 rounded-sm border '
        'relative overflow-hidden transition-all duration-200'
    )

    with ui.row().classes('w-full gap-4 mb-6 flex-wrap'):
        _render_stat_card('服务器总数', total, 'dns',
                          'var(--xf-accent)', stat_card_cls)
        _render_stat_card('在线', online_count, 'cloud_done',
                          STATUS_ONLINE, stat_card_cls)
        _render_stat_card('离线', offline_count, 'cloud_off',
                          STATUS_OFFLINE, stat_card_cls)
        _render_stat_card('未监控', unmonitored_count, 'visibility_off',
                          STATUS_IDLE, stat_card_cls)

    if not snapshots:
        filtering = bool(keyword) or group_filter != 'all'
        with ui.column().classes('w-full h-64 justify-center items-center'):
            ui.icon('inbox', size='4rem').style('color: var(--xf-text-muted);')
            ui.label('未找到匹配的服务器' if filtering else '暂无服务器') \
                .classes('text-sm font-bold').style(
                'color: var(--xf-text-muted);')
        return

    def sort_key(snap):
        # 离线（被监控且不在线的）优先级最高（0），然后是在线（1），未监控排最后（2）
        priority = 0 if snap['offline'] else (1 if snap['online'] else 2)
        return (priority, snap['name'].lower())

    snapshots.sort(key=sort_key)

    with ui.element('div').classes('w-full').style(
            'display: grid; '
            'grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); '
            'gap: 16px;'):
        for snap in snapshots:
            _render_server_card(snap)


def _render_stat_card(title, value, icon, color, cls):
    with ui.card().classes(cls).style(
            f'background: {_tint(color, 12)}; '
            f'border-color: color-mix(in srgb, {color} 30%, var(--xf-card-border)); '
            f'box-shadow: 0 6px 18px rgba(15,23,42,0.08);'):
        with ui.row().classes('items-center justify-between w-full'):
            with ui.column().classes('gap-0'):
                ui.label(title).classes(
                    'text-[11px] font-black uppercase tracking-wider '
                    'opacity-80').style('color: var(--xf-text-muted);')
                ui.label(str(value)).classes(
                    'text-3xl font-black tracking-tight').style(
                    f'color: {color};')
            with ui.element('div').classes(
                    'w-12 h-12 rounded-xl flex items-center '
                    'justify-center border').style(
                    f'background: {_tint(color, 20)}; '
                    f'border-color: color-mix(in srgb, {color} 30%, '
                    f'transparent);'):
                ui.icon(icon).classes('text-3xl').style(
                    f'color: {color}; opacity: 0.85;')


def _render_server_card(snap: dict):
    """渲染一台服务器的探针状态卡片。"""
    if snap['online']:
        status_color = STATUS_ONLINE
        status_text = 'ONLINE'
        status_icon = 'circle'
    elif snap['offline']:
        status_color = STATUS_OFFLINE
        status_text = 'OFFLINE'
        status_icon = 'circle'
    else:
        status_color = STATUS_IDLE
        status_text = '未监控'
        status_icon = 'radio_button_unchecked'

    border_accent = (
        f'color-mix(in srgb, {status_color} 25%, var(--xf-card-border))'
        if snap['monitored'] else 'var(--xf-card-border)'
    )
    # 离线卡压一层极淡的红，配合下面整排灰掉的 -- 指标，在网格里自然往后退
    card_bg = _tint(status_color, 5) if snap['offline'] else 'var(--xf-panel-bg)'

    with ui.card().classes(
            'p-0 gap-0 rounded-sm border overflow-hidden '
            'transition-all duration-200 hover:-translate-y-[1px]'
    ).style(
            f'background: {card_bg}; '
            f'border-color: {border_accent}; '
            f'box-shadow: 0 6px 18px rgba(15,23,42,0.10);'):
        _render_card_header(snap, status_color, status_text,
                            status_icon, border_accent)
        if not snap['monitored']:
            _render_card_unmonitored()
            _render_card_footer(snap, border_accent)
            return
        _render_card_sysinfo(snap)
        _render_card_metrics(snap)
        _render_card_footer(snap, border_accent)


async def _open_single_server(server_url: str):
    """点服务器名 → 进单机详情页。

    按 url 去 SERVERS_CACHE 里捞当前那个 dict，而不是用快照：快照是扁平副本，
    而 content_router 的 SINGLE 视图要的是缓存里的原对象（它按身份比较来判断
    「是不是同一台机器」，见 server_dialog 里那几处 current_data == ...）。
    """
    from app.ui.pages.content_router import refresh_content

    target = next((s for s in SERVERS_CACHE
                   if isinstance(s, dict) and s.get('url') == server_url), None)
    if not target:
        # 卡片渲染之后机器被删了
        safe_notify('服务器不存在，可能已被删除', 'warning')
        return

    await refresh_content('SINGLE', target)


def _render_card_header(snap, status_color, status_text,
                        status_icon, border_accent):
    # 根据离线状态调整 border 颜色，如果是离线，border 可能需要稍微明显一点？原代码是用 border_accent，这部分可以不动。
    
    with ui.row().classes(
            'w-full items-center justify-between px-4 py-3 border-b'
    ).style(f'border-color: {border_accent};'):
        with ui.row().classes('items-center gap-2 overflow-hidden'):
            os_name = snap['os'].lower() if snap['os'] else ''

            # 不用外链 SVG 图片做系统图标：探针数据同步上报时卡片需要重绘，img
            # 节点重建会触发浏览器重新加载/解码，表现为所有 Ubuntu/Debian 图标
            # 每 30 秒同步闪一下。纯 CSS/文字徽标没有网络加载，重绘也不会闪。
            os_mark = 'U' if 'ubuntu' in os_name else 'D' if 'debian' in os_name else 'C' if 'centos' in os_name else 'W' if 'windows' in os_name else 'L' if 'linux' in os_name else ''
            icon_color = 'var(--xf-text-subtle)' if snap['offline'] else '#E95420' if 'ubuntu' in os_name else '#D70A53' if 'debian' in os_name else '#262577' if 'centos' in os_name else '#0078D6' if 'windows' in os_name else 'var(--xf-accent)'

            if not snap['monitored'] or snap['offline']:
                ui.icon('dns').classes('text-base flex-shrink-0').style(f'color: {icon_color};')
            elif os_mark:
                ui.label(os_mark).classes(
                    'w-4 h-4 flex-shrink-0 rounded-[3px] text-[10px] font-black leading-none '
                    'flex items-center justify-center select-none'
                ).style(
                    f'color: white; background: {icon_color}; '
                    'width: 16px; height: 16px; min-width: 16px; min-height: 16px; '
                    'line-height: 16px;'
                )
            else:
                ui.icon('dns').classes('text-base flex-shrink-0').style(f'color: {icon_color};')
            
            # default leaf emoji as in screenshot
            ui.label('🍃').classes('text-sm flex-shrink-0').style('margin-right: -4px;')
                
            # 悬停变色只用 CSS：挂 mouseenter/mouseleave 回调的话每次划过鼠标
            # 都要走一趟服务端，代价和收益完全不成比例
            # Text should reflect offline status (e.g. grayed out if offline)
            text_color = 'var(--xf-text-subtle)' if snap['offline'] else 'var(--xf-text-strong)'
            name_label = ui.label(snap['name']).classes(
                'text-sm font-black truncate cursor-pointer hover:underline'
            ).style(f'color: {text_color};')
            # url 在这里就绑定好，否则闭包会共享循环变量，所有卡片都指向最后一台
            name_label.on('click', partial(_open_single_server, snap['url']))
            name_label.tooltip('点击查看单机详情')
            
        with ui.row().classes('items-center gap-1.5 flex-shrink-0'):
            uptime = snap['uptime'] if snap['uptime'] else '--'
            load_1 = f"{snap['load_1']:.1f}" if snap['load_1'] is not None else '--'
            
            uptime_color = '#ef4444' if snap['offline'] else ('#22c55e' if snap['online'] else 'var(--xf-text-subtle)')
            icon_uptime_color = '#ef4444' if snap['offline'] else ('#22c55e' if snap['online'] else 'var(--xf-text-subtle)')
            
            ui.icon('power_settings_new').classes('text-xs').style(f'color: {icon_uptime_color};')
            ui.label(uptime).classes('text-[11px] font-bold').style(f'color: {uptime_color};')
            
            # Load
            ui.icon('show_chart').classes('text-xs ml-1').style('color: var(--xf-text-subtle);')
            ui.label(load_1).classes('text-[11px] font-bold').style('color: var(--xf-text-subtle);')


def _last_report_info(snap: dict) -> tuple[str, str, str]:
    """卡片右下角的「最后上报」文案、颜色和精确时间 tooltip。"""
    last_push = _to_float(snap.get('last_push', 0))
    age_text = str(snap.get('data_age_text') or '').strip()
    if not last_push:
        return '未收到上报', 'var(--xf-text-subtle)', ''

    prefix = '上报' if snap.get('online') else '最后上报'
    color = '#22c55e' if snap.get('online') else (
        '#ef4444' if snap.get('offline') else 'var(--xf-text-subtle)'
    )
    exact_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_push))
    return f'{prefix} {age_text}', color, f'最后上报时间：{exact_time}'



def _render_card_unmonitored():
    with ui.column().classes(
            'w-full px-4 py-6 items-center justify-center gap-2'):
        ui.icon('visibility_off', size='2rem').style(
            'color: var(--xf-text-muted); opacity: 0.5;')
        ui.label('未安装探针').classes('text-xs font-bold').style(
            'color: var(--xf-text-muted);')


def _render_card_sysinfo(snap):
    """系统信息行。这里已移除，合并到 header 和 metrics 中，保持空实现避免报错"""
    pass


def _render_card_metrics(snap):
    """探针卡片指标区：用同一个 5 列 grid 固定标题和内容，避免缩放换行错位。"""
    unknown_cpu = snap['cpu_pct'] is None
    unknown_mem = snap['mem_pct'] is None
    unknown_disk = snap['disk_pct'] is None

    up_color = '#22c55e' if not snap['offline'] else 'var(--xf-text-subtle)'
    down_color = '#38bdf8' if not snap['offline'] else 'var(--xf-text-subtle)'

    title_class = 'text-[13px] font-black leading-none h-5 flex items-center justify-center'
    value_class = 'text-[13px] font-black leading-none'
    metric_grid_style = (
        'display: grid; '
        'grid-template-columns: repeat(5, minmax(0, 1fr)); '
        'column-gap: 8px; '
        'align-items: start;'
    )
    cell_class = 'min-w-0 w-full overflow-visible'
    traffic_row_class = (
        'min-w-0 w-full items-center justify-center leading-none '
        'whitespace-nowrap no-wrap'
    )
    traffic_row_style = (
        'display: grid; '
        'grid-template-columns: 18px minmax(0, max-content); '
        'column-gap: 6px; '
        'align-items: center; '
        'justify-content: center;'
    )

    def _traffic_row(arrow: str, text: str, color: str):
        # 箭头和值必须是同一个不可换行的小 grid，避免响应式压缩时拆开。
        with ui.row().classes(traffic_row_class).style(traffic_row_style):
            ui.label(arrow).classes(
                'text-[18px] font-black leading-none text-center'
            ).style(f'color: {color};')
            ui.label(text).classes(
                'text-[13px] font-bold leading-none whitespace-nowrap'
            ).style(f'color: {color};')

    with ui.column().classes('w-full px-4 py-4 gap-4'):
        # 标题和内容使用完全相同的 5 列 grid；不再用 flex row，防止 Network/I/O 被 wrap 到下一行。
        with ui.element('div').classes('w-full').style(metric_grid_style):
            for title in ('CPU', 'Mem', '磁盘', '网络', 'I/O'):
                ui.label(title).classes(f'{title_class} {cell_class}').style('color: var(--xf-text-strong);')

        with ui.element('div').classes('w-full').style(metric_grid_style):
            # CPU
            with ui.column().classes(f'items-center gap-3 {cell_class}'):
                _render_circular_progress(
                    snap['cpu_pct'],
                    '#22c55e' if not unknown_cpu and snap['cpu_pct'] < 90 else '#ef4444',
                    text=_format_cpu_pct(snap['cpu_pct']),
                    min_visible_pct=1 if not unknown_cpu else 0,
                )
                cores = f"{snap['cpu_cores']} C" if snap['cpu_cores'] else '--'
                ui.label(cores).classes(value_class).style('color: var(--xf-text-strong);')

            # Mem
            with ui.column().classes(f'items-center gap-3 {cell_class}'):
                _render_circular_progress(
                    snap['mem_pct'],
                    '#22c55e' if not unknown_mem and snap['mem_pct'] < 90 else '#ef4444',
                )
                if snap['mem_total_gb'] * 1024 < 1000 and snap['mem_total_gb'] < 1:
                    mem_text = f"{snap['mem_total_gb'] * 1024:.1f} M"
                else:
                    mem_text = f"{snap['mem_total_gb']:.1f} G"
                ui.label(mem_text).classes(value_class).style('color: var(--xf-text-strong);')

            # Disk
            with ui.column().classes(f'items-center gap-3 {cell_class}'):
                _render_circular_progress(
                    snap['disk_pct'],
                    '#22c55e' if not unknown_disk and snap['disk_pct'] < 90 else '#ef4444',
                )
                disk_text = f"{snap['disk_total_gb']:.1f} G"
                ui.label(disk_text).classes(value_class).style('color: var(--xf-text-strong);')

            # Network：两行固定在同一列内，箭头和值不允许拆行。
            with ui.column().classes(f'items-center justify-center gap-6 {cell_class}'):
                _traffic_row(
                    '↑',
                    _format_speed(snap['speed_out']) if snap['speed_out'] is not None else '0 B/s',
                    up_color,
                )
                _traffic_row(
                    '↓',
                    _format_speed(snap['speed_in']) if snap['speed_in'] is not None else '0 B/s',
                    down_color,
                )

            # I/O：与 Network 保持完全相同结构与高度。
            with ui.column().classes(f'items-center justify-center gap-6 {cell_class}'):
                _traffic_row(
                    '↑',
                    format_bytes(snap['net_out']) if snap['net_out'] else '0 B',
                    up_color,
                )
                _traffic_row(
                    '↓',
                    format_bytes(snap['net_in']) if snap['net_in'] else '0 B',
                    down_color,
                )

def _render_circular_progress(pct, color, *, text: str | None = None, min_visible_pct: float = 0.0):
    unknown = pct is None
    value_text = text if text is not None else ('--' if unknown else f'{pct:.0f}%')
    display_pct = 0 if unknown else max(float(pct), min_visible_pct)
    deg = int(display_pct * 3.6)
    
    bg_color = 'color-mix(in srgb, var(--xf-card-border) 70%, transparent)'
    active_color = 'var(--xf-text-subtle)' if unknown else color
    
    with ui.element('div').classes('relative flex items-center justify-center rounded-full').style(
        f'width: 54px; height: 54px; background: conic-gradient({active_color} {deg}deg, {bg_color} {deg}deg);'
    ):
        with ui.element('div').classes('absolute flex items-center justify-center rounded-full').style(
            'width: 44px; height: 44px; background: var(--xf-panel-bg);'
        ):
            ui.label(value_text).classes('text-[12px] font-black whitespace-nowrap leading-none').style(f'color: {active_color};')


def _render_card_footer(snap, border_accent):
    report_text, report_color, report_tip = _last_report_info(snap)
    with ui.row().classes('w-full justify-end items-center px-4 pb-3'):
        with ui.row().classes('items-center gap-1 whitespace-nowrap'):
            ui.icon('schedule').classes('text-[11px]').style(f'color: {report_color};')
            report_label = ui.label(report_text).classes(
                'text-[10px] font-bold leading-none').style(f'color: {report_color};')
            if report_tip:
                report_label.tooltip(report_tip)

def _render_speed_chip(icon, color, bps):
    style = (f'color: {color};' if bps is not None
             else 'color: var(--xf-text-subtle);')
    with ui.row().classes('items-center gap-1'):
        ui.icon(icon).classes('text-xs').style(style)
        ui.label(_format_speed(bps)).classes(
            'text-[11px] font-black font-mono').style(style)
