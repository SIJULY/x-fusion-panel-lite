"""探针监控页。

展示所有受监控服务器的实时状态概览——CPU、负载、内存、磁盘、网络速率等，
类似于哪吒探针 / ServerStatus 的面板效果。

数据来源是内存中的 PROBE_DATA_CACHE（探针 agent 定期推送），刷新间隔跟随
探针推送间隔（半分钟 ~ 半小时级）。前端每 30 秒醒一次，但只在内容真的变了
（有新推送、有机器跨过离线阈值、或推送年龄的显示档位变了）时才重绘——
推送间隔可能长达半小时，无条件重建整个卡片网格纯属白烧，还会打断鼠标 hover。

布局：
  顶部 — 搜索框 + 分组筛选，然后是统计概览（总服务器 / 在线 / 离线 / 未监控）
  中部 — 每台服务器一张卡片，按离线→在线→未监控排序，展示关键指标

配色一律走 --xf-* 主题变量，或 color-mix(语义色, var(--xf-panel-bg))，不要按
is_dark 把 rgba 写死：主题切换是 JS 热替换 CSS 变量（见 main_page 的
applyXFusionDomTheme），Python 端不会重新渲染，写死的颜色切主题后不会跟着变。
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


def _age_bucket(push_age):
    """推送年龄的显示档位，跟 format_push_age 的粒度对齐。

    format_push_age 在 90 秒内按秒、90 秒 ~ 90 分钟按分钟、再往上按 0.1 小时
    显示，所以只有跨过对应档位时文案才会变，没跨就不必重绘。
    """
    if push_age is None:
        return None
    if push_age < 90:
        return int(push_age // 30)
    if push_age < 5400:
        return int(push_age // 60)
    return int(push_age // 360)


def _probe_fingerprint() -> tuple:
    """页面内容的指纹：变了才值得重绘。

    除了各机器的推送时间戳，还必须带上「在线/离线」判定和推送年龄档位——这两个
    都随时间变化而与新数据无关，不带的话 agent 挂掉后卡片会永远停在 ONLINE，
    「N 分钟前」也不再走字。
    """
    now = time.time()
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
            _age_bucket(max(0.0, now - last_push) if last_push else None),
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
        'uptime': None if unknown else (str(probe.get('uptime') or '').strip() or None),
        'os': str(static.get('os') or '').strip(),
        'arch': str(static.get('arch') or '').strip(),
        'virt': virt,
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
            return
        _render_card_sysinfo(snap)
        _render_card_metrics(snap)
        _render_card_footer(snap, border_accent)


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
    """系统信息行：发行版 / 架构 / 虚拟化 / 数据年龄，下面单独一行 CPU 型号。"""
    chips = []
    if snap['os']:
        clean_os = (snap['os'].split('(')[0]
                    .replace('GNU/Linux', '')
                    .replace('  ', ' ').strip())
        if clean_os:
            chips.append(('computer', clean_os))
    if snap['arch']:
        chips.append(('memory', snap['arch']))
    if snap['virt']:
        chips.append(('layers', snap['virt']))
    if snap['data_age_text']:
        chips.append(('schedule', snap['data_age_text']))

    if not chips and not snap['cpu_model']:
        return

    with ui.column().classes('w-full px-4 pt-3 pb-1 gap-1'):
        if chips:
            with ui.row().classes('items-center gap-4 flex-wrap'):
                for icon, text in chips:
                    with ui.row().classes('items-center gap-1'):
                        ui.icon(icon).classes('text-xs').style(
                            'color: var(--xf-text-muted);')
                        ui.label(text).classes(
                            'text-[11px] font-bold').style(
                            'color: var(--xf-text-muted);')
        if snap['cpu_model']:
            # CPU 型号动不动就 40 多个字符，截断显示、完整挂 tooltip
            ui.label(snap['cpu_model']).classes(
                'text-[10px] font-bold truncate w-full').style(
                'color: var(--xf-text-subtle);').tooltip(snap['cpu_model'])


def _render_card_metrics(snap):
    """三条主指标全宽铺开：CPU / 内存 / 磁盘。

    原来是 2×2 等权重网格，「运行时间」和 CPU 一样大，信息层级是平的；现在
    运行时间降到页脚，主指标各占满一行，进度条更长也更好读。
    """
    cpu_sub = []
    if snap['cpu_cores']:
        cpu_sub.append(f'{snap["cpu_cores"]} 核')
    if snap['load_1'] is not None:
        cpu_sub.append(f'负载 {snap["load_1"]:.2f}')

    with ui.column().classes('w-full px-4 py-3 gap-2.5'):
        _render_metric_bar('CPU', snap['cpu_pct'], icon='speed',
                           sub=' · '.join(cpu_sub))
        _render_metric_bar('内存', snap['mem_pct'], icon='memory',
                           sub=_usage_text(snap['mem_used_gb'],
                                           snap['mem_total_gb']))
        _render_metric_bar('磁盘', snap['disk_pct'], icon='storage',
                           sub=_usage_text(snap['disk_used_gb'],
                                           snap['disk_total_gb']))


def _render_metric_bar(label, pct, sub='', icon='circle'):
    """一条全宽指标：左边标签 + 附注，右边数值，下面一根进度条。

    pct 为 None 表示「没有数据」（机器离线），显示 -- 和空条——不要显示 0%，
    那会被读成「很闲」。
    """
    unknown = pct is None
    color = 'var(--xf-text-subtle)' if unknown else _progress_color(pct)
    value_text = '--' if unknown else f'{pct:.1f}%'
    width = 0.0 if unknown else min(pct, 100.0)

    # 高水位给进度条加一点外发光，扫一眼就知道哪台快满了
    glow = ''
    if not unknown and pct >= 90:
        glow = f' box-shadow: 0 0 8px color-mix(in srgb, {color} 70%, transparent);'

    with ui.column().classes('w-full gap-1'):
        with ui.row().classes('items-center justify-between w-full gap-2'):
            with ui.row().classes('items-center gap-1.5 min-w-0'):
                ui.icon(icon).classes('text-xs flex-shrink-0').style(
                    f'color: {color};')
                ui.label(label).classes(
                    'text-[10px] font-black uppercase tracking-wider '
                    'flex-shrink-0').style('color: var(--xf-text-muted);')
                if sub:
                    ui.label(sub).classes(
                        'text-[10px] font-bold truncate').style(
                        'color: var(--xf-text-subtle);')
            ui.label(value_text).classes(
                'text-xs font-black flex-shrink-0').style(f'color: {color};')
        with ui.element('div').classes(
                'w-full h-2 rounded-full overflow-hidden'
        ).style('background: color-mix(in srgb, var(--xf-card-border) 70%, '
                'transparent);'):
            ui.element('div').classes(
                'h-full rounded-full transition-all duration-500'
            ).style(
                f'width: {width:.1f}%; '
                f'background: linear-gradient(90deg, '
                f'color-mix(in srgb, {color} 55%, transparent) 0%, '
                f'{color} 100%);{glow}')


def _render_card_footer(snap, border_accent):
    """页脚：实时上下行速率 + 运行时间，下面一行累计流量 / SWAP。"""
    with ui.column().classes('w-full px-4 pt-2 pb-3 gap-1 border-t').style(
            f'border-color: {border_accent};'):
        with ui.row().classes('items-center justify-between w-full gap-2'):
            # 实时速率：agent 每次推送都采样了 1 秒差值，之前页面一直没用上
            with ui.row().classes('items-center gap-3'):
                _render_speed_chip('arrow_downward', STATUS_ONLINE,
                                   snap['speed_in'])
                _render_speed_chip('arrow_upward', '#3b82f6',
                                   snap['speed_out'])
            if snap['uptime']:
                with ui.row().classes('items-center gap-1 min-w-0'):
                    ui.icon('timer').classes('text-xs flex-shrink-0').style(
                        'color: var(--xf-text-subtle);')
                    ui.label(snap['uptime']).classes(
                        'text-[10px] font-bold truncate').style(
                        'color: var(--xf-text-subtle);')

        # 累计流量是单调计数器，机器离线也照旧显示最后一次已知值
        with ui.row().classes('items-center gap-3 flex-wrap'):
            ui.label(f'总 ↓ {format_bytes(snap["net_in"])}').classes(
                'text-[10px] font-bold font-mono').style(
                'color: var(--xf-text-subtle);')
            ui.label(f'↑ {format_bytes(snap["net_out"])}').classes(
                'text-[10px] font-bold font-mono').style(
                'color: var(--xf-text-subtle);')
            if snap['swap_total_gb'] > 0:
                swap_used = snap['swap_used_gb']
                swap_text = '--' if swap_used is None else f'{swap_used:.1f}'
                ui.label(
                    f'SWAP {swap_text} / {snap["swap_total_gb"]:.1f} GB'
                ).classes('text-[10px] font-bold').style(
                    'color: var(--xf-text-subtle);')


def _render_speed_chip(icon, color, bps):
    style = (f'color: {color};' if bps is not None
             else 'color: var(--xf-text-subtle);')
    with ui.row().classes('items-center gap-1'):
        ui.icon(icon).classes('text-xs').style(style)
        ui.label(_format_speed(bps)).classes(
            'text-[11px] font-black font-mono').style(style)
