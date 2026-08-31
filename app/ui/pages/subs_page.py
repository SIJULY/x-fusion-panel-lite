"""订阅管理页。

两段式布局：**上半部分是独立节点**（手动粘贴的分享链接），**下半部分是订阅与组合**
（把节点打包成链接下发给客户端）。顺序是刻意的——先有节点才有订阅，从上往下读就是
用户的操作顺序；原来订阅在上、节点在下，新建一条订阅时得先往下滚去加节点、再滚回
上面来建订阅。

上半部分原先还在「独立节点」外面套了一层「节点池」区块。自从移除「面板节点」只读概览
之后，节点池里除了独立节点就什么都没有了（面板节点的正主是服务器管理页），两级标题说
的是同一件事，还各自挂了一个「批量导入」。现在塌成一层，计数挪到区块标题上。

订阅链接右侧的格式入口分成两层：常用四种（QUICK_FORMATS）直出成彩色图标按钮，其余
的收在「输出格式」菜单里，两边不重复。这里绕过一个弯——曾经把 Surge / Clash 图标全撤了，
因为它们和菜单里的同名项并存；但真正难用的是那排图标既没文字又是同一个灰色，认不出谁
是谁。所以现在保留图标、给每个不同的图标和语义色，改成让菜单不再重复它们。

配色统一走 _TONES 一份色板 + var(--xf-*) 主题变量，不再一处一个三元表达式。切主题时
main_page.toggle_theme 会整页重渲染这个视图（scope == 'SUBS'），所以这里按 is_dark
直接算死值是安全的，不依赖那套 className 字符串替换。
"""

import asyncio
import time

from nicegui import app, ui

from app.api.subscriptions import SUB_TARGETS
from app.core.state import (
    ADMIN_CONFIG,
    CURRENT_VIEW_STATE,
    INDEPENDENT_NODES_CACHE,
    SUB_ACCESS_STATS,
    SUBS_CACHE,
)
from app.services.sub_pipeline import build_node_lookup, resolve_sub_nodes
from app.storage.repositories import save_admin_config, save_independent_nodes, save_subs
from app.ui.common.notifications import safe_copy_to_clipboard, safe_notify, show_loading

# 格式菜单的排列顺序。SUB_TARGETS 是 dict，直接遍历顺序不好控制，而这里的顺序
# 决定用户第一眼看到哪几个，所以固定下来：常用的排前面。
TARGET_ORDER = ['clash', 'singbox', 'surge', 'quanx', 'loon', 'v2ray', 'clashr', 'ss']

# 直接摆在订阅链接右侧的常用格式：(target, 图标, 色板名)。
# 这四个覆盖绝大多数客户端，天天要复制的东西不该每次先展开一层菜单。
# 每个给了不同的图标 + 不同的语义色，是因为上一版那排图标全是同一个灰色、
# 又没有文字，扫一眼分不出谁是谁——病根是「没有区分度」，不是「有图标」。
QUICK_FORMATS = [
    ('surge', 'bolt', 'warn'),
    ('clash', 'cloud_queue', 'node'),
    ('v2ray', 'rocket_launch', 'group'),
    ('ss', 'send', 'ok'),
]
# 图标里已经有的，菜单里不再重复列一遍：同一条命令在相邻两处并存才是真的冗余。
QUICK_KEYS = [k for k, _, _ in QUICK_FORMATS]

# 语义色板：(前景, 底色, 描边)，深色一套 / 浅色一套。
# 之前这些颜色散在十几个 `... if is_dark else ...` 里，同一个语义在不同卡片上深浅不一致；
# 收敛到这里之后，「节点是蓝的、普通订阅是青的、组合是紫的、警告是琥珀、危险是玫红」
# 在整页范围内只有一处定义。
_TONES = {
    'node': (('#7dd3fc', 'rgba(14,165,233,0.13)', 'rgba(56,189,248,0.34)'),
             ('#0369a1', 'rgba(14,165,233,0.10)', 'rgba(14,165,233,0.28)')),
    'sub': (('#67e8f9', 'rgba(6,182,212,0.13)', 'rgba(34,211,238,0.34)'),
            ('#0e7490', 'rgba(6,182,212,0.10)', 'rgba(6,182,212,0.28)')),
    'group': (('#c4b5fd', 'rgba(139,92,246,0.15)', 'rgba(167,139,250,0.36)'),
              ('#6d28d9', 'rgba(139,92,246,0.10)', 'rgba(139,92,246,0.28)')),
    'ok': (('#6ee7b7', 'rgba(16,185,129,0.13)', 'rgba(52,211,153,0.34)'),
           ('#047857', 'rgba(16,185,129,0.10)', 'rgba(16,185,129,0.28)')),
    'warn': (('#fcd34d', 'rgba(245,158,11,0.14)', 'rgba(251,191,36,0.36)'),
             ('#b45309', 'rgba(245,158,11,0.12)', 'rgba(245,158,11,0.32)')),
    'danger': (('#fda4af', 'rgba(244,63,94,0.13)', 'rgba(251,113,133,0.34)'),
               ('#be123c', 'rgba(244,63,94,0.10)', 'rgba(244,63,94,0.28)')),
    'muted': (('#94a3b8', 'rgba(148,163,184,0.10)', 'rgba(148,163,184,0.26)'),
              ('#64748b', 'rgba(100,116,139,0.08)', 'rgba(100,116,139,0.24)')),
}

# 卡片阴影放在 class 里而不是 style 里：inline style 会盖掉 hover:shadow-*，
# 放 class 才能有悬停微抬。
_CARD_SHADOW = 'shadow-[0_6px_18px_rgba(15,23,42,0.10)] hover:shadow-[0_10px_26px_rgba(15,23,42,0.18)]'


def tone(name, is_dark):
    fg, bg, border = _TONES.get(name, _TONES['muted'])[0 if is_dark else 1]
    return {'fg': fg, 'bg': bg, 'border': border}


def chip(text, name='muted', is_dark=True, icon=None, tip=None):
    """一枚小标签。卡片上的元信息全部走它，粗细 / 间距 / 圆角只有这一处定义。"""
    t = tone(name, is_dark)
    with ui.row().classes('items-center gap-1 px-2 py-[3px] rounded-sm border shrink-0') \
            .style(f"background: {t['bg']}; border-color: {t['border']}; color: {t['fg']};") as row:
        if icon:
            ui.icon(icon).classes('text-[13px]')
        ui.label(str(text)).classes('text-[11px] font-bold font-mono leading-none whitespace-nowrap')
    if tip:
        row.tooltip(tip)
    return row


def stat_pill(icon, label, value, name, is_dark, tip=None):
    """页头右侧的总览药丸：一眼看清节点 / 订阅各有多少。"""
    t = tone(name, is_dark)
    with ui.row().classes('items-center gap-2 px-3 py-1.5 rounded-sm border shrink-0') \
            .style(f"background: {t['bg']}; border-color: {t['border']};") as row:
        ui.icon(icon).classes('text-[15px]').style(f"color: {t['fg']};")
        ui.label(str(value)).classes('text-base font-black leading-none').style(f"color: {t['fg']};")
        ui.label(label).classes('text-[11px] font-bold leading-none whitespace-nowrap') \
            .style('color: var(--xf-text-muted);')
    if tip:
        row.tooltip(tip)
    return row


def section_header(icon, title, desc, is_dark, name='node', count=None):
    """区块标题栏。返回右侧操作区，调用方 `with ...:` 往里塞按钮。

    `count` 给了就在标题旁边挂一枚计数 chip。原先计数只出现在区块内部那层小标题
    （group_label）上，于是「区块标题 + 小标题」两行说的是同一件事——计数上提之后，
    只装一样东西的区块不必再套第二层标题。
    """
    t = tone(name, is_dark)
    with ui.row().classes('w-full items-center justify-between gap-3 flex-wrap mb-3 pb-2.5 border-b') \
            .style('border-color: var(--xf-card-border);'):
        with ui.row().classes('items-center gap-3 min-w-0'):
            with ui.element('div').classes('w-9 h-9 rounded-sm flex items-center justify-center border shrink-0') \
                    .style(f"background: {t['bg']}; border-color: {t['border']}; color: {t['fg']};"):
                ui.icon(icon).classes('text-[18px]')
            with ui.column().classes('gap-0 min-w-0'):
                with ui.row().classes('items-center gap-2 min-w-0'):
                    ui.label(title).classes('text-lg font-black tracking-wide leading-tight') \
                        .style('color: var(--xf-text-strong);')
                    if count is not None:
                        chip(f'{count} 条' if count else '0', name if count else 'muted', is_dark)
                if desc:
                    ui.label(desc).classes('text-[11px] font-medium leading-tight') \
                        .style('color: var(--xf-text-subtle);')
        actions = ui.row().classes('items-center gap-2 shrink-0 flex-wrap')
    return actions


def group_label(icon, text, count, name, is_dark, note=''):
    """区块内部的小分组标题（普通订阅 / 组合订阅 / 独立节点）。"""
    t = tone(name, is_dark)
    with ui.row().classes('w-full items-center gap-2 flex-wrap mb-2'):
        ui.icon(icon).classes('text-[15px]').style(f"color: {t['fg']};")
        ui.label(text).classes('text-xs font-black tracking-wide').style('color: var(--xf-text-strong);')
        chip(f'{count} 条' if count else '0', name if count else 'muted', is_dark)
        if note:
            ui.label(note).classes('text-[10px] font-medium').style('color: var(--xf-text-subtle);')


def action_btn(label, icon, on_click, name, is_dark, tip=None):
    t = tone(name, is_dark)
    b = ui.button(label, icon=icon, on_click=on_click).props('flat dense size=sm') \
        .classes('rounded-sm font-black border px-3 whitespace-nowrap') \
        .style(f"background: {t['bg']}; border-color: {t['border']}; color: {t['fg']};")
    if tip:
        b.tooltip(tip)
    return b


def icon_btn(icon, on_click, name, is_dark, tip=None, size='sm'):
    t = tone(name, is_dark)
    b = ui.button(icon=icon, on_click=on_click).props(f'flat dense round size={size}') \
        .style(f"color: {t['fg']};")
    if tip:
        b.tooltip(tip)
    return b


def card_shell(name, is_dark):
    """卡片外壳的 (classes, style)。左侧 4px 竖条按语义上色，扫一眼就知道是哪一类。

    注意 border-left-color 必须排在 border-color 后面：inline style 里后写的赢，
    否则四边同色，左边那条竖线就白设了。
    """
    t = tone(name, is_dark)
    cls = f'w-full flex flex-col gap-2 p-3 mb-2 rounded-sm border border-l-4 transition-shadow {_CARD_SHADOW}'
    sty = (f"background: var(--xf-panel-bg); border-color: var(--xf-card-border); "
           f"border-left-color: {t['fg']};")
    return cls, sty


def empty_state(icon, title, desc, is_dark, name='muted', btn_label=None, btn_icon='add', on_click=None):
    """空态。原来是 h-64 的大虚线框，一空就把整页顶下去；这里压到 py-7 并带上入口按钮。"""
    t = tone(name, is_dark)
    with ui.column().classes('w-full items-center justify-center gap-2 py-7 px-4 mb-3 rounded-sm border border-dashed') \
            .style('background: var(--xf-panel-bg); border-color: var(--xf-card-border);'):
        ui.icon(icon).classes('text-[30px]').style(f"color: {t['fg']}; opacity: 0.75;")
        ui.label(title).classes('text-sm font-black').style('color: var(--xf-text-muted);')
        if desc:
            ui.label(desc).classes('text-[11px] font-medium text-center').style('color: var(--xf-text-subtle);')
        if btn_label and on_click:
            action_btn(btn_label, btn_icon, on_click, name, is_dark)


def thin_hint(text):
    """比空态更轻的一行提示，用在「订阅有、但这一类没有」的场景。"""
    ui.label(text).classes('w-full text-[11px] font-medium py-2.5 px-3 mb-3 rounded-sm border border-dashed') \
        .style('background: var(--xf-panel-bg); border-color: var(--xf-card-border); color: var(--xf-text-subtle);')


def confirm_dialog(title, body_lines, confirm_label, on_confirm, is_dark, name='danger', icon='warning'):
    """统一的二次确认弹窗。

    原来三个确认框（删订阅 / 删独立节点 / 清理失效）各写一遍渐变头 + 按钮，样式还不完全
    一致；现在只有这一处，语义靠 name 换色。
    """
    t = tone(name, is_dark)
    with ui.dialog() as d, ui.card().classes('w-[400px] p-0 gap-0 overflow-hidden rounded-sm border') \
            .style(f"background: var(--xf-panel-bg); border-color: {t['border']}; "
                   f"box-shadow: 0 18px 48px rgba(2,6,23,0.45);"):
        with ui.row().classes('w-full items-center gap-3 p-4 border-b') \
                .style(f"background: {t['bg']}; border-color: {t['border']};"):
            ui.icon(icon).classes('text-[20px]').style(f"color: {t['fg']};")
            ui.label(title).classes('font-black text-base tracking-wide').style(f"color: {t['fg']};")

        with ui.column().classes('w-full p-4 gap-2').style('background: var(--xf-panel-bg);'):
            for line in body_lines:
                if not line:
                    continue
                ui.label(line).classes('text-xs font-medium leading-relaxed').style('color: var(--xf-text-muted);')

        with ui.row().classes('w-full justify-end items-center gap-2 p-3 border-t') \
                .style('background: var(--xf-soft-bg); border-color: var(--xf-card-border);'):
            ui.button('取消', on_click=d.close).props('flat dense size=sm') \
                .classes('rounded-sm font-bold px-3').style('color: var(--xf-text-muted);')

            async def go():
                d.close()
                await on_confirm()

            ui.button(confirm_label, on_click=go).props('flat dense size=sm') \
                .classes('rounded-sm font-black border px-4') \
                .style(f"background: {t['bg']}; border-color: {t['border']}; color: {t['fg']};")

    d.open()
    return d


def ordered_targets():
    """按 TARGET_ORDER 排，漏掉的（以后新增的）补在后面，不会静默丢格式。"""
    keys = [k for k in TARGET_ORDER if k in SUB_TARGETS]
    keys += [k for k in SUB_TARGETS if k not in keys]
    return keys


def qr_data_uri(text):
    """把文本转成 PNG 二维码的 data URI。失败返回 None（不抛，页面不能因此白屏）。"""
    try:
        import base64
        import io

        import qrcode

        img = qrcode.make(str(text or ''))
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception:
        return None


def open_qr_dialog(title, url_pairs):
    """一个弹窗里切换格式看二维码——手机扫码直接导入，不用在手机上敲长链接。"""
    is_dark = bool(app.storage.user.get('is_dark', True))
    labels = [lbl for lbl, _ in url_pairs]
    url_map = dict(url_pairs)

    with ui.dialog() as d, ui.card().classes('w-[380px] p-0 gap-0 overflow-hidden rounded-sm border') \
            .style('background: var(--xf-panel-bg); border-color: var(--xf-card-border); '
                   'box-shadow: 0 18px 48px rgba(2,6,23,0.45);'):
        with ui.row().classes('w-full justify-between items-center p-4 border-b') \
                .style('background: var(--xf-soft-bg); border-color: var(--xf-card-border);'):
            ui.label(f'扫码导入 · {title}').classes('text-base font-black tracking-wide truncate') \
                .style('color: var(--xf-text-strong);')
            ui.button(icon='close', on_click=d.close).props('flat round dense') \
                .style('color: var(--xf-text-muted);')

        with ui.column().classes('w-full p-4 gap-3 items-center').style('background: var(--xf-panel-bg);'):
            # 下拉框排在二维码上面。render 是闭包、调用时才查名字，
            # 所以它定义在 on_change 之后没问题。
            ui.select(labels, value=labels[0], label='输出格式',
                      on_change=lambda e: render(e.value)) \
                .props('outlined dense dark color=cyan standout bg-color="[#050b14]"' if is_dark else 'outlined dense color=blue') \
                .classes('w-full')
            holder = ui.column().classes('w-full items-center gap-2')

            def render(label):
                holder.clear()
                url = url_map.get(label, '')
                with holder:
                    data = qr_data_uri(url)
                    if data:
                        ui.image(data).style('width: 220px; height: 220px') \
                            .classes('rounded-sm bg-white p-2 border border-slate-300')
                    else:
                        ui.label('二维码生成失败（qrcode 库不可用），可直接复制下面的链接') \
                            .classes('text-xs text-center').style(f"color: {tone('warn', is_dark)['fg']};")
                    with ui.row().classes('w-full items-center gap-2 p-2 rounded-sm border cursor-pointer') \
                            .style('background: var(--xf-code-bg); border-color: var(--xf-card-border);') \
                            .on('click', lambda u=url: safe_copy_to_clipboard(u)):
                        ui.label(url).classes('text-[10px] font-mono flex-grow break-all') \
                            .style('color: var(--xf-text-strong);')
                        ui.icon('content_copy', size='xs').style('color: var(--xf-text-muted);')

            render(labels[0])

        d.open()


def sub_url_pairs(origin, token):
    """这条订阅的全部可用链接：原始（按 UA 自适应）+ 8 种显式格式。"""
    pairs = [('原始链接（客户端自适应）', f"{origin}/sub/{token}")]
    for t in ordered_targets():
        pairs.append((SUB_TARGETS[t], f"{origin}/get/sub/{t}/{token}"))
    return pairs


def access_text(token):
    """把访问统计写成一句人话；没被拉取过返回 None。"""
    entry = SUB_ACCESS_STATS.get(token) or {}
    count = entry.get('count') or 0
    if not count:
        return None

    ago = time.time() - (entry.get('last_at') or 0)
    if ago < 60:
        when = '刚刚'
    elif ago < 3600:
        when = f'{int(ago // 60)} 分钟前'
    elif ago < 86400:
        when = f'{int(ago // 3600)} 小时前'
    else:
        when = f'{int(ago // 86400)} 天前'

    ua = (entry.get('last_ua') or '').strip()
    parts = [f'拉取 {count} 次', f'最后 {when}']
    if ua:
        parts.append(ua[:28])
    return ' · '.join(parts)


def link_bar(url, is_dark, buttons=None, tip='点击复制'):
    """代码底色的链接条：整条可点复制，右侧留给格式按钮。"""
    with ui.row().classes('w-full items-center gap-2 p-2 rounded-sm border justify-between') \
            .style('background: var(--xf-code-bg); border-color: var(--xf-card-border);'):
        with ui.row().classes('items-center gap-2 flex-grow min-w-0 cursor-pointer') \
                .on('click', lambda u=url: safe_copy_to_clipboard(u)) as clickable:
            ui.icon('link').classes('text-[14px] shrink-0').style('color: var(--xf-accent);')
            ui.label(url).classes('text-[11px] font-mono font-bold truncate select-all') \
                .style('color: var(--xf-text-strong);')
            # 复制提示图标改成常驻（原先只在没有右侧按钮时才画）：整条本来就能点，
            # 缺了这个暗示就只是看着像一段纯文本。放在可点区域内部，点它也等于点整条。
            ui.icon('content_copy').classes('text-[13px] shrink-0 opacity-60') \
                .style('color: var(--xf-text-muted);')
        clickable.tooltip(tip)

        if buttons:
            with ui.row().classes('items-center gap-1.5 shrink-0'):
                buttons()


async def load_subs_view():
    global CURRENT_VIEW_STATE
    from app.ui.dialogs.sub_dialogs import open_advanced_sub_editor

    CURRENT_VIEW_STATE['scope'] = 'SUBS'
    CURRENT_VIEW_STATE['data'] = None
    CURRENT_VIEW_STATE['page'] = 1
    app.storage.user['last_view_scope'] = 'SUBS'
    app.storage.user['last_view_data'] = None
    app.storage.user['last_view_page'] = 1

    from app.ui.pages.content_router import content_container

    show_loading(content_container)

    origin = ""

    db_url = ADMIN_CONFIG.get('manager_base_url', '').strip().rstrip('/')
    if db_url and not ('127.0.0.1' in db_url or 'localhost' in db_url):
        origin = db_url

    if not origin:
        try:
            origin = await ui.run_javascript('return window.location.origin', timeout=3.0)
        except:
            pass

    if not origin or origin == 'null':
        try:
            req = ui.context.client.request
            real_host = req.headers.get('X-Forwarded-Host') or req.headers.get('host')
            real_proto = req.headers.get('X-Forwarded-Proto') or req.url.scheme
            if real_host:
                origin = f"{real_proto}://{real_host}"
        except:
            pass

    if not origin:
        origin = "http://x-fusion-panel"

    if origin and "x-fusion-panel" not in origin:
        if ADMIN_CONFIG.get('manager_base_url') != origin:
            ADMIN_CONFIG['manager_base_url'] = origin
            asyncio.create_task(save_admin_config())

    is_dark = bool(app.storage.user.get('is_dark', True))

    content_container.clear()
    content_container.classes(remove='justify-center items-center overflow-hidden p-6',
                              add='h-full overflow-y-auto p-4 pl-6 justify-start')
    content_container.style('background-color: var(--xf-bg-main);')

    # 用管线的索引当「有效 key」的唯一标准，页面显示的有效数就等于订阅真正能解析出的
    # 节点数。原来这里只扫 SERVERS_CACHE，独立节点不在其中，于是引用了独立节点的订阅
    # 会被算成「失效」——数字是错的，而现在多了一键清理，照着错数字清会误删。
    lookup = build_node_lookup()
    all_active_keys = set(lookup.keys())

    # 索引连同原始下标一起留着：删除走的是 `del SUBS_CACHE[i]`，分组后仍要用真实下标。
    normal_subs = [(i, s) for i, s in enumerate(SUBS_CACHE) if s.get('type') != 'collection']
    collections = [(i, s) for i, s in enumerate(SUBS_CACHE) if s.get('type') == 'collection']

    # 面板节点只需要一个总数：订阅能引用多少条面板节点，用在页头统计和独立节点区块的
    # 副标题上（「另有面板节点 N 条由服务器管理页同步」）。原来这里按服务器聚合出
    # name / host / total / on，只为「面板节点」那块只读概览的 chip 列表服务；概览已移除
    # （节点的正主是服务器管理页），聚合也就没有存在意义了。
    # lookup 里 srv 为 None 的是独立节点，单独一节展示，不计入这里。
    panel_total = sum(1 for _node, _host, srv in lookup.values() if srv)

    def open_batch_import():
        from app.ui.dialogs.sub_dialogs import open_batch_import_dialog
        open_batch_import_dialog()

    def open_add_independent_node():
        from app.ui.dialogs.sub_dialogs import open_independent_node_editor
        open_independent_node_editor(None)

    with content_container:
        # ───────────── 页头 + 总览 ─────────────
        with ui.row().classes('w-full items-center justify-between gap-4 flex-wrap mb-5 pb-3 border-b') \
                .style('border-color: var(--xf-card-border);'):
            with ui.row().classes('items-center gap-3 min-w-0'):
                with ui.element('div').classes('w-10 h-10 rounded-sm flex items-center justify-center '
                                               'border relative overflow-hidden shrink-0') \
                        .style('background: var(--xf-code-bg); border-color: var(--xf-card-border); '
                               'color: var(--xf-accent); box-shadow: 0 4px 12px rgba(15,23,42,0.12);'):
                    ui.element('div').classes('absolute inset-0').style('background: var(--xf-accent-soft);')
                    ui.icon('rss_feed').classes('text-[18px] drop-shadow-[0_0_5px_currentColor]')
                with ui.column().classes('gap-0 min-w-0'):
                    ui.label('订阅管理').classes('text-2xl font-black tracking-wide leading-tight') \
                        .style('color: var(--xf-text-strong);')
                    ui.label('上面维护独立节点，下面把节点打包成订阅下发给客户端') \
                        .classes('text-[11px] font-medium leading-tight').style('color: var(--xf-text-subtle);')

            # 「独立节点」那枚药丸删了：它和下面独立节点区块标题上的计数是同一个数字，
            # 而那个区块就贴在页头下面一行。面板 / 独立的拆分仍在「可用节点」的 tooltip 里。
            with ui.row().classes('items-center gap-2 flex-wrap'):
                stat_pill('lan', '可用节点', len(all_active_keys), 'node', is_dark,
                          f'面板 {panel_total} + 独立 {len(INDEPENDENT_NODES_CACHE)}')
                stat_pill('rss_feed', '订阅', len(normal_subs), 'sub', is_dark, '手动勾选节点的普通订阅')
                stat_pill('layers', '组合', len(collections), 'group', is_dark, '合并多条订阅的组合订阅')

        # ═════════════ ① 独立节点 ═════════════
        # 原先是「节点池」区块里面再套一层「独立节点」小标题。节点池自从移除「面板节点」
        # 只读概览之后就只剩独立节点一样内容，两级标题说的是同一件事，而且两处各挂了一个
        # 「批量导入」。现在合成一层，批量导入只留区块标题栏这一个。
        with section_header('bolt', '独立节点',
                            f'手动粘贴的分享链接，可被任意订阅引用 · '
                            f'另有面板节点 {panel_total} 条由服务器管理页同步，不用在这里维护',
                            is_dark, 'node', count=len(INDEPENDENT_NODES_CACHE)):
            action_btn('批量导入', 'playlist_add', open_batch_import, 'muted', is_dark, '一次粘贴多条分享链接')
            action_btn('添加独立节点', 'add', open_add_independent_node, 'node', is_dark, '手填一条分享链接')

        if not INDEPENDENT_NODES_CACHE:
            # 空态不再重复挂「批量导入」：两个入口就在上面这行标题栏里，隔着 40px 再放一个
            # 只是把空框撑得更高，而这个框已经占了首屏最大的一块
            empty_state('hub', '还没有独立节点',
                        '用上面的「批量导入 / 添加独立节点」把机场或自建的分享链接粘进来，'
                        '就能和面板节点一起打包成订阅',
                        is_dark, 'node')
        else:
            for idx, inode in enumerate(INDEPENDENT_NODES_CACHE):
                link = inode.get('_raw_link', '') or ''
                protocol = (link.split('://')[0] if '://' in link else 'unknown').lower()
                ikey = f"independent|{inode.get('id')}"
                used_by = [s.get('name', '未命名') for s in SUBS_CACHE if ikey in (s.get('nodes', []) or [])]

                cls, sty = card_shell('node', is_dark)
                with ui.element('div').classes(cls).style(sty):
                    with ui.row().classes('w-full items-center justify-between gap-3 flex-wrap'):
                        with ui.row().classes('items-center gap-2 min-w-0 flex-wrap'):
                            chip(protocol, 'node', is_dark, tip=f"节点 ID: {inode.get('id', 'N/A')}")
                            ui.label(inode.get('remark') or '未命名节点') \
                                .classes('text-sm font-black tracking-wide truncate') \
                                .style('color: var(--xf-text-strong);')
                            if used_by:
                                chip(f'{len(used_by)} 条订阅在用', 'sub', is_dark, icon='link',
                                     tip='、'.join(used_by))
                            else:
                                chip('未被引用', 'muted', is_dark, icon='link_off', tip='还没有任何订阅勾选它')

                        with ui.row().classes('items-center gap-1.5 shrink-0'):
                            def edit_inode(node=inode):
                                from app.ui.dialogs.sub_dialogs import open_independent_node_editor
                                open_independent_node_editor(node)

                            action_btn('编辑', 'edit', edit_inode, 'muted', is_dark, '改名 / 换链接')

                            def del_inode(i=idx, node=inode):
                                ik = f"independent|{node.get('id')}"
                                refs = [s.get('name', '未命名') for s in SUBS_CACHE
                                        if ik in (s.get('nodes', []) or [])]
                                lines = ['删掉之后，引用它的订阅会少掉这个节点。']
                                if refs:
                                    lines.append('⚠️ 以下订阅正在使用它：' + '、'.join(refs))

                                async def apply():
                                    del INDEPENDENT_NODES_CACHE[i]
                                    await save_independent_nodes()
                                    await load_subs_view()
                                    safe_notify('已删除独立节点', 'positive')

                                confirm_dialog('确定删除此独立节点？', lines, '删除', apply,
                                               is_dark, 'danger', 'delete_forever')

                            icon_btn('delete', del_inode, 'danger', is_dark, '删除这个独立节点')

                    link_bar(link, is_dark, tip='点击复制节点链接')

        # ═════════════ ② 订阅与组合 ═════════════
        ui.element('div').classes('w-full h-px my-6').style('background: var(--xf-card-border);')

        with section_header('rss_feed', '订阅与组合', '给客户端用的链接，节点从上面挑',
                            is_dark, 'sub', count=len(SUBS_CACHE)):
            action_btn('新建订阅', 'add', lambda: open_advanced_sub_editor(None), 'ok', is_dark,
                       '勾选节点建普通订阅，或建一条合并多条订阅的组合')

        def render_sub_card(idx, sub):
            is_collection = sub.get('type') == 'collection'
            token = sub.get('token', '')
            node_keys = sub.get('nodes', []) or []
            saved_keys = set(node_keys)
            valid_count = len(saved_keys & all_active_keys)
            total_count = len(saved_keys)
            dead_keys = [k for k in node_keys if k not in all_active_keys]

            try:
                delivered = len(resolve_sub_nodes(sub, lookup=lookup))
            except Exception:
                delivered = None

            members = sub.get('members', []) or []
            member_names = []
            broken_members = 0
            for mt in members:
                m = next((s for s in SUBS_CACHE if s.get('token') == mt), None)
                if m:
                    member_names.append(m.get('name') or '未命名')
                else:
                    broken_members += 1
                    member_names.append(f'已失效({mt[:6]})')

            name = 'group' if is_collection else 'sub'
            cls, sty = card_shell(name, is_dark)
            raw_url = f"{origin}/sub/{token}"

            with ui.element('div').classes(cls).style(sty):
                with ui.row().classes('w-full items-start justify-between gap-3 flex-wrap'):
                    with ui.column().classes('gap-2 min-w-0 flex-grow'):
                        with ui.row().classes('items-center gap-2 flex-wrap'):
                            ui.label(sub.get('name') or '未命名订阅') \
                                .classes('text-base font-black tracking-wide') \
                                .style('color: var(--xf-text-strong);')
                            chip('组合' if is_collection else '普通', name, is_dark,
                                 icon='layers' if is_collection else 'rss_feed',
                                 tip='节点来自多个成员订阅的合并' if is_collection else '手动勾选的节点')

                        with ui.row().classes('items-center gap-1.5 flex-wrap'):
                            if is_collection:
                                chip(f'成员 {len(members)}', 'group', is_dark, icon='account_tree',
                                     tip='、'.join(member_names) or '还没有成员')
                            else:
                                chip(f'节点 {valid_count} / {total_count}',
                                     'ok' if valid_count else 'muted', is_dark, icon='lan',
                                     tip='有效 / 已勾选')

                            if delivered is not None:
                                # 有效数 ≠ 实际下发数，说明筛选规则把节点刷掉了。不点开预览也能看见，
                                # 免得「明明选了 20 个客户端只有 3 个」还得去猜。
                                mismatch = (not is_collection) and delivered != valid_count
                                chip(f'下发 {delivered}',
                                     'warn' if (mismatch or not delivered) else 'ok', is_dark,
                                     icon='download',
                                     tip='按筛选 / 改名规则算完后真正给客户端的节点数'
                                         + ('（筛选规则刷掉了一部分）' if mismatch else ''))

                            if dead_keys:
                                dead_info = "、".join([k.split("|")[1] if "|" in k else k for k in dead_keys])
                                chip(f'失效 {len(dead_keys)}', 'danger', is_dark, icon='link_off',
                                     tip=f'节点所在服务器已被删除，或 x-ui 里的 ID 变了: {dead_info}')
                            if broken_members:
                                chip(f'成员失效 {broken_members}', 'danger', is_dark, icon='warning',
                                     tip='引用的成员订阅已被删除')

                            acc = access_text(token)
                            if acc:
                                chip(acc, 'muted', is_dark, icon='history',
                                     tip='访问统计只放内存，面板重启后清零')

                        if is_collection and member_names:
                            ui.label('合并: ' + ' + '.join(member_names)) \
                                .classes('text-[11px] font-mono') \
                                .style('color: var(--xf-text-subtle);')

                    with ui.row().classes('items-center gap-1.5 shrink-0'):
                        if dead_keys:
                            def do_clean(s=sub, dead=list(dead_keys)):
                                async def apply():
                                    old = list(s.get('nodes', []) or [])
                                    kept = [k for k in old if k in all_active_keys]
                                    s['nodes'] = kept
                                    await save_subs()
                                    await load_subs_view()
                                    safe_notify(f'已清理 {len(old) - len(kept)} 个失效节点', 'positive')

                                confirm_dialog(
                                    f'清理 {len(dead)} 个失效节点？',
                                    ['这些节点所在的服务器已被删除，或在 x-ui 里的 ID 变了，'
                                     '订阅里只剩一个连不上的死引用。',
                                     '清理只剔除死引用，其余节点的顺序保持不变。'],
                                    '确认清理', apply, is_dark, 'warn', 'cleaning_services')

                            dead_info = "、".join([k.split("|")[1] if "|" in k else k for k in dead_keys])
                            tip_text = f'剔除指向已删服务器 / 已变 ID 的节点: {dead_info}' if len(dead_info) < 50 else '剔除指向已删服务器 / 已变 ID 的节点 (悬浮查看失效 ID)'
                            btn = action_btn(f'清理失效 {len(dead_keys)}', 'cleaning_services', do_clean,
                                       'warn', is_dark, tip_text)
                            if len(dead_info) >= 50:
                                btn.tooltip(f'失效节点 ID: {dead_info}')

                        action_btn('管理', 'tune', lambda s=sub: open_advanced_sub_editor(s), 'muted', is_dark,
                                   '选节点 / 筛地区 / 重命名 / 预览')

                        def do_del(i=idx, s=sub):
                            tok = s.get('token')
                            refs = [x.get('name', '未命名') for x in SUBS_CACHE
                                    if x.get('type') == 'collection' and tok in (x.get('members', []) or [])]
                            lines = ['删除后这条链接立即失效，已经导入的客户端会拉不到节点。']
                            if refs:
                                lines.append('⚠️ 以下组合订阅正在引用它，删掉后它们会少掉这批节点：'
                                             + '、'.join(refs))

                            async def apply():
                                del SUBS_CACHE[i]
                                await save_subs()
                                await load_subs_view()
                                safe_notify('已删除', 'positive')

                            confirm_dialog('确定删除此订阅？', lines, '删除', apply,
                                           is_dark, 'danger', 'delete_forever')

                        icon_btn('delete', do_del, 'danger', is_dark, '删除这条订阅')

                def format_buttons():
                    # 四种常用格式（Surge / Clash / V2Ray / Shadowsocks）直接摆出来，
                    # 其余的收在「输出格式」里。分界线是「点击频率」而不是「格式多少」。
                    for t, ic, tn in QUICK_FORMATS:
                        if t not in SUB_TARGETS:
                            continue          # 上游哪天删了某个 target 就静默跳过，不炸页面
                        url = f"{origin}/get/sub/{t}/{token}"
                        icon_btn(ic, lambda u=url: safe_copy_to_clipboard(u), tn, is_dark,
                                 f"复制 {SUB_TARGETS[t]} 链接")

                    icon_btn('qr_code_2',
                             lambda n=sub.get('name', '订阅'), p=sub_url_pairs(origin, token):
                                 open_qr_dialog(n, p),
                             'sub', is_dark, '扫码导入（可切换格式）')

                    # 菜单入口是带字带下拉箭头的按钮，不是一个省略号——藏在 ⋯ 后面等于没有。
                    # 样式跟 action_btn 保持一致。
                    rest = [t for t in ordered_targets() if t not in QUICK_KEYS]
                    tm = tone('muted', is_dark)
                    fmt_btn = ui.button('输出格式').props('flat dense size=sm icon-right=expand_more') \
                        .classes('rounded-sm font-black border px-2.5 whitespace-nowrap') \
                        .style(f"background: {tm['bg']}; border-color: {tm['border']}; color: {tm['fg']};")
                    fmt_btn.tooltip('其余格式：' + ' / '.join(SUB_TARGETS[t] for t in rest) + '，以及原始链接')

                    with fmt_btn:
                        # 底色 / 边框不用管：main_page 里有一条全局 .q-menu 规则拿
                        # !important 设了 --xf-popup-*，这里再写 inline 也是输
                        with ui.menu().props('auto-close').classes('rounded-sm'):
                            for t in rest:
                                url = f"{origin}/get/sub/{t}/{token}"
                                ui.menu_item(f"复制 {SUB_TARGETS[t]} 链接",
                                             on_click=lambda u=url: safe_copy_to_clipboard(u)) \
                                    .classes('text-xs')
                            ui.separator()
                            ui.menu_item('复制原始链接（自适应）',
                                         on_click=lambda u=raw_url: safe_copy_to_clipboard(u)) \
                                .classes('text-xs')

                link_bar(raw_url, is_dark, format_buttons, '点击复制原始链接（客户端自适应）')

        if not SUBS_CACHE:
            empty_state('rss_feed', '还没有订阅',
                        '点「新建订阅」勾几个节点，客户端填上生成的链接就能用',
                        is_dark, 'ok', '新建订阅', 'add', lambda: open_advanced_sub_editor(None))
        else:
            group_label('rss_feed', '普通订阅', len(normal_subs), 'sub', is_dark, '手动勾选节点')
            if not normal_subs:
                thin_hint('暂无普通订阅。普通订阅是直接勾选节点的那种，组合订阅的成员必须是它。')
            for idx, sub in normal_subs:
                render_sub_card(idx, sub)

            group_label('layers', '组合订阅', len(collections), 'group', is_dark, '合并多条订阅的节点')
            if not collections:
                thin_hint('暂无组合订阅。在「新建订阅」里打开组合模式，就能把几条订阅合并成一条链接下发。')
            for idx, sub in collections:
                render_sub_card(idx, sub)
