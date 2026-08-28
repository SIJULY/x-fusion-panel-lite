"""订阅管理页。

布局参考 sub-store：**上半部分是节点池**（面板同步来的节点 + 手动加的独立节点），
**下半部分是订阅与组合**（把节点打包成链接下发给客户端）。顺序是刻意的——先有节点才
有订阅，从上往下读就是用户的操作顺序。

## 这一版 UI 重构解决的三件事

1. **节点池不再是「密密麻麻、大小不一」的标签云。** 上一版把 N 台服务器渲染成 N 枚
   宽度随名字长短变化的 chip，塞进 `flex-wrap` 行里，再用 `max-h` 硬切——名字一长一短
   就参差不齐，最后一行还被切掉一半，看着像渲染坏了。现在换成 **CSS Grid 等宽等高
   单元格**（`repeat(auto-fill, minmax(198px, 1fr))` + 固定 30px 行高），列对齐行对齐，
   名字超长就省略号截断，滚动区底部用渐隐收口而不是硬切。

2. **配色从 7 种身份色收敛到 1 种。** 上一版「节点蓝 / 订阅青 / 组合紫 / 成功绿 /
   警告琥珀 / 危险玫红 / 灰」七个色相同屏出现，没有主次。现在只有 `var(--xf-accent)`
   一个强调色承担全部**身份**，ok / warn / danger 三色**只表示状态**。普通订阅和组合
   订阅靠左侧导轨的**实线 / 断续线**区分，不再各占一个色相——形状分类比颜色分类耐看，
   也让真正需要注意的红色重新变得显眼。

3. **彩色药丸换成读数带。** 页头 4 枚各带底色描边的药丸合并成一条发丝分隔的读数带；
   订阅卡片上那排 `节点 x/y`、`下发 n`、`失效 m` 同样合并成一条行内读数。信息量不变，
   视觉噪音少一个数量级。

## 实现方式

样式改成**作用域 CSS**（`scoped_style()` 注入 `<style>`，选择器全部以 `.xf-subs` 打头），
不再是几百个 inline style 字符串拼接。这样才能用伪元素（HUD 四角刻线）、`:hover` 过渡和
Grid，而这些才是「科技感」的实际来源——不是渐变和光晕。样式节点随
`content_container.clear()` 一起销毁，重复渲染不会堆积。

主题切换时 main_page.toggle_theme 会整页重渲染本视图（scope == 'SUBS'），所以按 is_dark
直接算死值是安全的，不依赖那套 className 字符串替换。

**功能与上一版完全一致**，这次只改外观：按钮、弹窗、统计、链接、tooltip 一个没动。
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

# 状态色。**只有三个，且只表示状态**——身份一律用 var(--xf-accent)。
# 上一版这里有 7 个色相且兼作身份色，同屏五六种颜色，红色反而不显眼了。
_STATE = {
    'ok': ('#34d399', '#047857'),
    'warn': ('#fbbf24', '#b45309'),
    'danger': ('#fb7185', '#be123c'),
}

# CSS 里大括号太多，用 f-string 得全部转义，容易改错。改成哨兵占位 + replace()。
_CSS = """
.xf-subs{
  --s-ok:__OK__; --s-warn:__WARN__; --s-danger:__DANGER__;
  --s-line:var(--xf-card-border);
  --s-shadow:__SHADOW__; --s-shadow-hi:__SHADOWHI__;
  width:100%; display:flex; flex-direction:column;
  /* 等宽数字。网格能对齐、读数带的数字不跳动，全靠这一行 */
  font-variant-numeric:tabular-nums;
}

/* 眉标：小号大写拉丁字 + 向右延伸的细线。把「区块 / 分组 / 卡片」三层扁平的
   标题压出清晰层级，同时是科技面板最省事的一种识别度来源 */
.xf-subs .eyebrow{display:flex;align-items:center;gap:10px;width:100%}
.xf-subs .eyebrow .lt{font-size:9px;font-weight:800;letter-spacing:.22em;
  color:var(--xf-text-subtle);white-space:nowrap;line-height:1}
.xf-subs .eyebrow .rule{flex:1;height:1px;
  background:linear-gradient(90deg,var(--s-line),transparent)}

/* HUD 面：四角刻线。本页「科技感」的主要载体——只用 1px 描边，
   不跟内容抢注意力，也不会像渐变那样几个月后看着过时 */
.xf-subs .hud{position:relative;background:var(--xf-code-bg);
  border:1px solid var(--s-line);border-radius:2px}
.xf-subs .hud::before,.xf-subs .hud::after{content:'';position:absolute;
  width:10px;height:10px;pointer-events:none}
.xf-subs .hud::before{top:-1px;left:-1px;
  border-top:1px solid var(--xf-accent);border-left:1px solid var(--xf-accent)}
.xf-subs .hud::after{bottom:-1px;right:-1px;
  border-bottom:1px solid var(--xf-accent);border-right:1px solid var(--xf-accent)}

/* 读数带：一个容器 + 发丝分隔线，替掉页头那 4 枚各自带底色和描边的彩色药丸 */
.xf-subs .readout{display:flex;align-items:stretch;border:1px solid var(--s-line);
  background:var(--xf-code-bg);border-radius:2px;overflow:hidden}
.xf-subs .readout .cell{display:flex;flex-direction:column;gap:4px;padding:6px 14px;
  border-left:1px solid var(--s-line);min-width:0}
.xf-subs .readout .cell:first-child{border-left:0}
.xf-subs .readout .v{font-size:17px;font-weight:900;line-height:1}
.xf-subs .readout .k{font-size:9px;font-weight:700;letter-spacing:.14em;
  color:var(--xf-text-subtle);white-space:nowrap;line-height:1}

/* 行内读数：订阅卡片上那排指标，同样合并成一条 */
.xf-subs .inline{display:inline-flex;align-items:center;flex-wrap:wrap;
  border:1px solid var(--s-line);background:var(--xf-code-bg);border-radius:2px;
  overflow:hidden;align-self:flex-start;max-width:100%}
.xf-subs .inline .cell{display:flex;align-items:baseline;gap:5px;padding:5px 11px;
  border-left:1px solid var(--s-line);white-space:nowrap}
.xf-subs .inline .cell:first-child{border-left:0}
.xf-subs .inline .k{font-size:9px;font-weight:700;letter-spacing:.1em;
  color:var(--xf-text-subtle)}
.xf-subs .inline .v{font-size:12px;font-weight:900;line-height:1}

/* 节点网格：等宽等高。这是「大小不一致 / 错乱」的正解——
   名字长短不再影响单元格宽度，列和行都严格对齐 */
.xf-subs .ngrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(198px,1fr));
  gap:6px;width:100%}
.xf-subs .ncell{display:flex;align-items:center;gap:8px;height:30px;padding-right:9px;
  background:var(--xf-panel-bg);border:1px solid var(--s-line);border-radius:2px;
  overflow:hidden;transition:border-color .14s,background .14s}
.xf-subs .ncell:hover{background:var(--xf-hover-bg);
  border-color:color-mix(in srgb,var(--xf-accent) 45%,var(--s-line))}
.xf-subs .ncell .bar{flex:0 0 3px;align-self:stretch;background:var(--xf-accent)}
.xf-subs .ncell .nm{flex:1;min-width:0;font-size:11px;font-weight:700;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--xf-text-strong)}
.xf-subs .ncell .ct{flex:0 0 auto;font-size:11px;font-weight:900;color:var(--xf-accent)}
.xf-subs .ncell.off .bar{background:var(--xf-text-subtle);opacity:.55}
.xf-subs .ncell.off .ct{color:var(--xf-text-subtle)}

/* 滚动区底部渐隐。上一版是 max-h 硬切，最后一行被切掉一半，看着像渲染坏了；
   渐隐让「下面还有」变成有意的设计而不是缺陷 */
.xf-subs .nscroll{position:relative;width:100%}
.xf-subs .nscroll .pane{max-height:138px;overflow-y:auto;width:100%;padding-bottom:2px}
.xf-subs .nscroll .fade{position:absolute;left:0;right:0;bottom:0;height:20px;
  pointer-events:none;background:linear-gradient(180deg,transparent,var(--xf-code-bg))}

/* 卡片：左侧 2px 导轨。普通订阅实线、组合订阅断续线——
   用形状区分而不是再引入一个色相 */
.xf-subs .card{position:relative;display:flex;flex-direction:column;gap:9px;
  padding:11px 14px;background:var(--xf-panel-bg);border:1px solid var(--s-line);
  border-radius:2px;box-shadow:var(--s-shadow);
  transition:transform .16s,box-shadow .16s,border-color .16s}
.xf-subs .card:hover{transform:translateY(-1px);box-shadow:var(--s-shadow-hi);
  border-color:color-mix(in srgb,var(--xf-accent) 32%,var(--s-line))}
.xf-subs .card .rail{position:absolute;left:0;top:0;bottom:0;width:2px;
  background:var(--xf-accent)}
.xf-subs .card .rail.dash{background:repeating-linear-gradient(180deg,
  var(--xf-accent) 0 6px,transparent 6px 11px)}

/* 链接条 */
.xf-subs .lbar{display:flex;align-items:center;gap:8px;padding:5px 8px;
  background:var(--xf-code-bg);border:1px solid var(--s-line);border-radius:2px;
  transition:border-color .14s}
.xf-subs .lbar:hover{border-color:color-mix(in srgb,var(--xf-accent) 40%,var(--s-line))}

/* 徽标：默认中性，只有需要强调时才 .on。上一版每种语义一个底色，一屏太花 */
.xf-subs .tag{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;
  border:1px solid var(--s-line);border-radius:2px;background:var(--xf-soft-bg);
  color:var(--xf-text-muted);font-size:10px;font-weight:800;white-space:nowrap;
  line-height:1.5}
.xf-subs .tag.on{background:var(--xf-accent-soft);color:var(--xf-accent);
  border-color:color-mix(in srgb,var(--xf-accent) 40%,var(--s-line))}

/* 操作按钮：描边式，底色跟着面板走，只用文字色区分语义 */
.xf-subs .act{border:1px solid;border-radius:2px;font-size:11px;font-weight:800;
  padding:0 10px;background:var(--xf-panel-bg);transition:background .14s}
.xf-subs .act:hover{background:var(--xf-hover-bg)}

/* 空态：从 py-7 的大虚线框压成一行，不再一空就占掉首屏三分之一 */
.xf-subs .empty{display:flex;align-items:center;gap:11px;width:100%;padding:11px 14px;
  border:1px dashed var(--s-line);border-radius:2px;background:var(--xf-code-bg)}
"""


def state_color(name, is_dark):
    """状态色取值。name 不在表里（含 None）就回落到中性色，调用方不用先判空。"""
    pair = _STATE.get(name)
    if not pair:
        return 'var(--xf-text-muted)'
    return pair[0 if is_dark else 1]


def scoped_style(is_dark):
    """注入本页作用域样式。

    用 ui.html 挂在 content_container 里而不是 ui.add_css / add_head_html：那两个往
    <head> 塞，而 head_html 只在首次加载时下发——本页是单页应用里反复重渲染的视图，
    第二次进来根本不会生效，反复调用还会越堆越多。挂在容器里则随 clear() 一起销毁。

    **`sanitize=False` 是必须的**：ui.html 默认走浏览器 setHTML() 消毒，`<style>`
    会被整个剥掉，页面静默变成完全无样式（dashboard.py:602 内嵌 <style> 时同样如此）。
    这里的内容是写死的 CSS 常量，不含任何用户输入，关掉消毒是安全的。

    外面那层 div 用 hidden 只是不让它在 flex 列里白占一个 gap；<style> 的规则跟祖先
    的 display 无关，照样对整个文档生效。
    """
    shadow = ('0 1px 0 rgba(255,255,255,0.03) inset, 0 6px 18px rgba(2,6,23,0.45)'
              if is_dark else '0 1px 2px rgba(15,23,42,0.05), 0 6px 16px rgba(148,163,184,0.13)')
    shadow_hi = ('0 1px 0 rgba(255,255,255,0.05) inset, 0 12px 30px rgba(2,6,23,0.6)'
                 if is_dark else '0 2px 4px rgba(15,23,42,0.07), 0 12px 26px rgba(148,163,184,0.2)')
    css = (_CSS
           .replace('__OK__', _STATE['ok'][0 if is_dark else 1])
           .replace('__WARN__', _STATE['warn'][0 if is_dark else 1])
           .replace('__DANGER__', _STATE['danger'][0 if is_dark else 1])
           .replace('__SHADOWHI__', shadow_hi)
           .replace('__SHADOW__', shadow))
    ui.html(f'<style>{css}</style>', sanitize=False).classes('hidden')


# ───────────────────────── 视觉基元 ─────────────────────────

def eyebrow(latin):
    """小号大写拉丁眉标 + 延伸细线。"""
    with ui.row().classes('eyebrow'):
        ui.label(latin).classes('lt')
        ui.element('div').classes('rule')


def tag(text, icon=None, on=False, color=None, tip=None):
    """一枚小徽标。默认中性，on=True 用强调色，color 显式覆盖（状态色）。"""
    el = ui.row().classes('tag on' if on else 'tag')
    if color:
        el.style(f'color:{color}; border-color:color-mix(in srgb,{color} 38%,var(--xf-card-border));')
    with el:
        if icon:
            ui.icon(icon).classes('text-[12px]')
        ui.label(str(text))
    if tip:
        el.tooltip(tip)
    return el


def readout(items, is_dark):
    """页头总览读数带。items: [(标签, 值, 状态名或None, tip)]。"""
    with ui.element('div').classes('readout'):
        for label, value, st, tip in items:
            cell = ui.element('div').classes('cell')
            with cell:
                ui.label(str(value)).classes('v') \
                    .style(f'color:{state_color(st, is_dark) if st else "var(--xf-accent)"};')
                ui.label(label).classes('k')
            if tip:
                cell.tooltip(tip)


def inline_readout(items, is_dark):
    """卡片上的行内读数带。items: [(标签, 值, 状态名或None, tip)]。"""
    with ui.element('div').classes('inline'):
        for label, value, st, tip in items:
            cell = ui.element('div').classes('cell')
            with cell:
                ui.label(label).classes('k')
                ui.label(str(value)).classes('v') \
                    .style(f'color:{state_color(st, is_dark) if st else "var(--xf-text-strong)"};')
            if tip:
                cell.tooltip(tip)


def section_head(icon, latin, title, desc):
    """区块标题栏：眉标 + 图标 + 标题 + 说明。返回右侧操作区供 `with` 塞按钮。"""
    with ui.column().classes('w-full gap-2 mb-3'):
        eyebrow(latin)
        with ui.row().classes('w-full items-center justify-between gap-3 flex-wrap'):
            with ui.row().classes('items-center gap-2.5 min-w-0'):
                with ui.element('div').classes('w-7 h-7 flex items-center justify-center shrink-0') \
                        .style('background:var(--xf-accent-soft);color:var(--xf-accent);'
                               'border:1px solid color-mix(in srgb,var(--xf-accent) 34%,'
                               'var(--xf-card-border));border-radius:2px;'):
                    ui.icon(icon).classes('text-[16px]')
                with ui.column().classes('gap-0.5 min-w-0'):
                    ui.label(title).classes('text-[15px] font-black leading-none tracking-wide') \
                        .style('color:var(--xf-text-strong);')
                    if desc:
                        ui.label(desc).classes('text-[10px] font-medium leading-none') \
                            .style('color:var(--xf-text-subtle);')
            actions = ui.row().classes('items-center gap-2 shrink-0 flex-wrap')
    return actions


def group_label(icon, text, count, note=''):
    """区块内的小分组标题（独立节点 / 普通订阅 / 组合订阅）。"""
    with ui.row().classes('w-full items-center gap-2 flex-wrap mb-2 mt-1'):
        ui.icon(icon).classes('text-[14px]').style('color:var(--xf-accent);')
        ui.label(text).classes('text-[12px] font-black tracking-wide') \
            .style('color:var(--xf-text-strong);')
        tag(count, on=bool(count))
        if note:
            ui.label(note).classes('text-[10px] font-medium').style('color:var(--xf-text-subtle);')


def act(label, icon, on_click, tip=None, st=None, is_dark=True):
    """描边式操作按钮。st 给状态色，None 用强调色。"""
    col = state_color(st, is_dark) if st else 'var(--xf-accent)'
    b = ui.button(label, icon=icon, on_click=on_click).props('flat dense no-caps size=sm') \
        .classes('act') \
        .style(f'color:{col};border-color:color-mix(in srgb,{col} 34%,var(--xf-card-border));')
    if tip:
        b.tooltip(tip)
    return b


def icon_btn(icon, on_click, tip=None, st=None, is_dark=True, size='sm'):
    col = state_color(st, is_dark) if st else 'var(--xf-text-muted)'
    b = ui.button(icon=icon, on_click=on_click).props(f'flat dense round size={size}') \
        .style(f'color:{col};')
    if tip:
        b.tooltip(tip)
    return b


def empty_row(icon, title, desc, btn_label=None, btn_icon='add', on_click=None, is_dark=True):
    """一行式空态。上一版是 h-64 / py-7 的大虚线框，一空就把整页顶下去。"""
    with ui.element('div').classes('empty'):
        ui.icon(icon).classes('text-[20px] shrink-0').style('color:var(--xf-accent);opacity:.7;')
        with ui.column().classes('gap-0.5 min-w-0 flex-grow'):
            ui.label(title).classes('text-[12px] font-black leading-tight') \
                .style('color:var(--xf-text-muted);')
            if desc:
                ui.label(desc).classes('text-[10px] font-medium leading-tight') \
                    .style('color:var(--xf-text-subtle);')
        if btn_label and on_click:
            with ui.row().classes('shrink-0'):
                act(btn_label, btn_icon, on_click, is_dark=is_dark)


def thin_hint(text):
    """比空态更轻的一行提示，用在「订阅有、但这一类没有」的场景。"""
    ui.label(text).classes('w-full text-[10px] font-medium py-2.5 px-3 mb-2 border border-dashed') \
        .style('background:var(--xf-code-bg);border-color:var(--xf-card-border);'
               'color:var(--xf-text-subtle);border-radius:2px;')


def link_bar(url, buttons=None, tip='点击复制', wrap=False):
    """代码底色的链接条：整条可点复制，右侧留给格式按钮。

    wrap=True 时链接换行显示完整内容（二维码弹窗那种窄容器要看全 URL），
    默认单行省略号——卡片上一行放不下整条链接，截断比撑破布局好。
    """
    with ui.row().classes('lbar w-full justify-between'):
        with ui.row().classes('items-center gap-2 flex-grow min-w-0 cursor-pointer') \
                .on('click', lambda u=url: safe_copy_to_clipboard(u)) as clickable:
            ui.icon('link').classes('text-[13px] shrink-0').style('color:var(--xf-accent);')
            ui.label(url).classes('text-[11px] font-mono font-bold select-all '
                                  + ('break-all' if wrap else 'truncate')) \
                .style('color:var(--xf-text-strong);')
        clickable.tooltip(tip)

        if buttons:
            with ui.row().classes('items-center gap-0.5 shrink-0'):
                buttons()
        else:
            # 没有格式按钮时也要有个「可以复制」的视觉暗示，否则整条只是看着像纯文本
            ui.icon('content_copy').classes('text-[13px] shrink-0').style('color:var(--xf-text-muted);')


def confirm_dialog(title, body_lines, confirm_label, on_confirm, is_dark, st='danger', icon='warning'):
    """统一的二次确认弹窗。语义靠 st 换色，样式只有这一处定义。"""
    col = state_color(st, is_dark)
    with ui.dialog() as d, ui.card().classes('w-[400px] p-0 gap-0 overflow-hidden border') \
            .style(f'background:var(--xf-panel-bg);border-radius:2px;'
                   f'border-color:color-mix(in srgb,{col} 40%,var(--xf-card-border));'
                   f'box-shadow:0 18px 48px rgba(2,6,23,0.5);'):
        with ui.row().classes('w-full items-center gap-3 px-4 py-3 border-b') \
                .style(f'background:var(--xf-code-bg);border-color:var(--xf-card-border);'):
            ui.element('div').classes('w-0.5 self-stretch shrink-0').style(f'background:{col};')
            ui.icon(icon).classes('text-[18px]').style(f'color:{col};')
            ui.label(title).classes('font-black text-[14px] tracking-wide').style(f'color:{col};')

        with ui.column().classes('w-full px-4 py-3.5 gap-2').style('background:var(--xf-panel-bg);'):
            for line in body_lines:
                if not line:
                    continue
                ui.label(line).classes('text-[11px] font-medium leading-relaxed') \
                    .style('color:var(--xf-text-muted);')

        with ui.row().classes('w-full justify-end items-center gap-2 px-3 py-2.5 border-t') \
                .style('background:var(--xf-soft-bg);border-color:var(--xf-card-border);'):
            ui.button('取消', on_click=d.close).props('flat dense no-caps size=sm') \
                .classes('font-bold px-3 text-[11px]').style('color:var(--xf-text-muted);')

            async def go():
                d.close()
                await on_confirm()

            ui.button(confirm_label, on_click=go).props('flat dense no-caps size=sm') \
                .classes('act px-4') \
                .style(f'color:{col};border-color:color-mix(in srgb,{col} 40%,var(--xf-card-border));')

    d.open()
    return d


# ───────────────────────── 数据 / 链接工具 ─────────────────────────

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

    with ui.dialog() as d, ui.card().classes('xf-subs w-[380px] p-0 gap-0 overflow-hidden border') \
            .style('background:var(--xf-panel-bg);border-color:var(--xf-card-border);'
                   'border-radius:2px;box-shadow:0 18px 48px rgba(2,6,23,0.5);'):
        # 弹窗挂在 body 下、不在 content_container 里，所以要自带一份作用域样式，
        # 否则 .act / .lbar 这些类在这里是空的
        scoped_style(is_dark)

        with ui.row().classes('w-full justify-between items-center px-4 py-3 border-b') \
                .style('background:var(--xf-code-bg);border-color:var(--xf-card-border);'):
            with ui.column().classes('gap-1 min-w-0'):
                eyebrow('QR IMPORT')
                ui.label(title).classes('text-[14px] font-black tracking-wide truncate') \
                    .style('color:var(--xf-text-strong);')
            ui.button(icon='close', on_click=d.close).props('flat round dense') \
                .style('color:var(--xf-text-muted);')

        with ui.column().classes('w-full p-4 gap-3 items-center').style('background:var(--xf-panel-bg);'):
            # 下拉框排在二维码上面。render 是闭包、调用时才查名字，
            # 所以它定义在 on_change 之后没问题。
            ui.select(labels, value=labels[0], label='输出格式',
                      on_change=lambda e: render(e.value)) \
                .props('outlined dense options-dense'
                       + (' dark color=cyan' if is_dark else ' color=blue')) \
                .classes('w-full')
            holder = ui.column().classes('w-full items-center gap-2')

            def render(label):
                holder.clear()
                url = url_map.get(label, '')
                with holder:
                    data = qr_data_uri(url)
                    if data:
                        ui.image(data).style('width:220px;height:220px;border-radius:2px;') \
                            .classes('bg-white p-2 border border-slate-300')
                    else:
                        ui.label('二维码生成失败（qrcode 库不可用），可直接复制下面的链接') \
                            .classes('text-[11px] text-center') \
                            .style(f'color:{state_color("warn", is_dark)};')
                    link_bar(url, tip='点击复制这个格式的链接', wrap=True)

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


# ───────────────────────── 主视图 ─────────────────────────

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

    # 面板节点按服务器聚合。lookup 里 srv 为 None 的就是独立节点，单独一节展示。
    panel_servers = {}
    for _key, (node, host, srv) in lookup.items():
        if not srv:
            continue
        row = panel_servers.setdefault(srv.get('url'), {
            'name': srv.get('name') or '未命名服务器', 'host': host, 'total': 0, 'on': 0,
        })
        row['total'] += 1
        if node.get('enable', True):
            row['on'] += 1
    panel_total = sum(v['total'] for v in panel_servers.values())
    panel_on = sum(v['on'] for v in panel_servers.values())

    def open_batch_import():
        from app.ui.dialogs.sub_dialogs import open_batch_import_dialog
        open_batch_import_dialog()

    def open_add_independent_node():
        from app.ui.dialogs.sub_dialogs import open_independent_node_editor
        open_independent_node_editor(None)

    with content_container:
        with ui.column().classes('xf-subs gap-0'):
            scoped_style(is_dark)

            # ───────────── 页头 + 总览 ─────────────
            with ui.row().classes('w-full items-center justify-between gap-4 flex-wrap mb-5 pb-3.5 border-b') \
                    .style('border-color:var(--xf-card-border);'):
                with ui.row().classes('items-center gap-3 min-w-0'):
                    with ui.element('div').classes('hud w-10 h-10 flex items-center justify-center shrink-0') \
                            .style('color:var(--xf-accent);'):
                        ui.icon('rss_feed').classes('text-[19px]')
                    with ui.column().classes('gap-1 min-w-0'):
                        eyebrow('SUBSCRIPTION MANAGER')
                        ui.label('订阅管理').classes('text-[21px] font-black tracking-wide leading-none') \
                            .style('color:var(--xf-text-strong);')
                        ui.label('上面维护节点池，下面把节点打包成订阅下发给客户端') \
                            .classes('text-[10px] font-medium leading-none') \
                            .style('color:var(--xf-text-subtle);')

                readout([
                    ('可用节点', len(all_active_keys), None,
                     f'面板 {panel_total} + 独立 {len(INDEPENDENT_NODES_CACHE)}'),
                    ('独立节点', len(INDEPENDENT_NODES_CACHE), None, '手动粘贴的分享链接'),
                    ('订阅', len(normal_subs), None, '手动勾选节点的普通订阅'),
                    ('组合', len(collections), None, '合并多条订阅的组合订阅'),
                ], is_dark)

            # ═════════════ ① 节点池 ═════════════
            with section_head('hub', 'NODE POOL', '节点池',
                              f'订阅能引用的全部节点 · 面板 {panel_total} + 独立 '
                              f'{len(INDEPENDENT_NODES_CACHE)}'):
                act('批量导入', 'playlist_add', open_batch_import, '一次粘贴多条分享链接',
                    is_dark=is_dark)
                act('添加独立节点', 'add', open_add_independent_node, '手填一条分享链接',
                    is_dark=is_dark)

            # 面板节点：只读概览。真正的增删在「服务器管理」里，这里给的是「我手上有多少料」。
            if panel_servers:
                with ui.element('div').classes('hud w-full flex flex-col gap-2.5 p-3 mb-4'):
                    with ui.row().classes('w-full items-center justify-between gap-2 flex-wrap'):
                        with ui.row().classes('items-center gap-2 flex-wrap'):
                            ui.icon('dns').classes('text-[14px]').style('color:var(--xf-accent);')
                            ui.label('面板节点').classes('text-[11px] font-black tracking-wide') \
                                .style('color:var(--xf-text-strong);')
                            tag(f'{len(panel_servers)} 台服务器')
                            tag(f'启用 {panel_on} / 共 {panel_total}',
                                color=state_color('ok' if panel_on else 'warn', is_dark),
                                tip='x-ui 里被禁用的入站默认不下发')
                        ui.label('由服务器管理同步，此处只读').classes('text-[10px] font-medium') \
                            .style('color:var(--xf-text-subtle);')

                    # 等宽等高网格 + 底部渐隐。上一版是 flex-wrap 的 chip 云 + max-h 硬切，
                    # 名字长短决定宽度，参差不齐；这里宽度由 grid 决定，名字超长走省略号。
                    with ui.element('div').classes('nscroll'):
                        with ui.element('div').classes('pane'):
                            with ui.element('div').classes('ngrid'):
                                for url, info in panel_servers.items():
                                    cell = ui.element('div').classes(
                                        'ncell' if info['on'] else 'ncell off')
                                    with cell:
                                        ui.element('div').classes('bar')
                                        ui.label(info['name']).classes('nm')
                                        ui.label(str(info['total'])).classes('ct')
                                    cell.tooltip(f"{url} · 出口 {info['host'] or '—'} · "
                                                 f"启用 {info['on']} / 共 {info['total']}")
                        ui.element('div').classes('fade')

            # 独立节点
            group_label('bolt', '独立节点', len(INDEPENDENT_NODES_CACHE),
                        '手动粘贴的分享链接，可被任意订阅引用')

            if not INDEPENDENT_NODES_CACHE:
                empty_row('hub', '还没有独立节点',
                          '把机场或自建的分享链接粘进来，就能和面板节点一起打包成订阅',
                          '批量导入', 'playlist_add', open_batch_import, is_dark)
            else:
                with ui.column().classes('w-full gap-2'):
                    for idx, inode in enumerate(INDEPENDENT_NODES_CACHE):
                        link = inode.get('_raw_link', '') or ''
                        protocol = (link.split('://')[0] if '://' in link else 'unknown').lower()
                        ikey = f"independent|{inode.get('id')}"
                        used_by = [s.get('name', '未命名') for s in SUBS_CACHE
                                   if ikey in (s.get('nodes', []) or [])]

                        with ui.element('div').classes('card'):
                            ui.element('div').classes('rail')
                            with ui.row().classes('w-full items-center justify-between gap-3 flex-wrap'):
                                with ui.row().classes('items-center gap-2 min-w-0 flex-wrap'):
                                    tag(protocol, on=True, tip=f"节点 ID: {inode.get('id', 'N/A')}")
                                    ui.label(inode.get('remark') or '未命名节点') \
                                        .classes('text-[13px] font-black tracking-wide truncate') \
                                        .style('color:var(--xf-text-strong);')
                                    if used_by:
                                        tag(f'{len(used_by)} 条订阅在用', icon='link',
                                            tip='、'.join(used_by))
                                    else:
                                        tag('未被引用', icon='link_off', tip='还没有任何订阅勾选它')

                                with ui.row().classes('items-center gap-1.5 shrink-0'):
                                    def edit_inode(node=inode):
                                        from app.ui.dialogs.sub_dialogs import open_independent_node_editor
                                        open_independent_node_editor(node)

                                    act('编辑', 'edit', edit_inode, '改名 / 换链接', is_dark=is_dark)

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

                                    icon_btn('delete', del_inode, '删除这个独立节点', 'danger', is_dark)

                            link_bar(link, tip='点击复制节点链接')

            # ═════════════ ② 订阅与组合 ═════════════
            ui.element('div').classes('w-full h-px my-6').style('background:var(--xf-card-border);')

            with section_head('rss_feed', 'SUBSCRIPTIONS', '订阅与组合',
                              '给客户端用的链接，节点从上面的节点池里挑'):
                act('新建订阅', 'add', lambda: open_advanced_sub_editor(None),
                    '勾选节点建普通订阅，或建一条合并多条订阅的组合', is_dark=is_dark)

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

                raw_url = f"{origin}/sub/{token}"

                with ui.element('div').classes('card'):
                    # 普通订阅实线导轨、组合订阅断续导轨——形状区分，不再多占一个色相
                    ui.element('div').classes('rail dash' if is_collection else 'rail')

                    with ui.row().classes('w-full items-start justify-between gap-3 flex-wrap'):
                        with ui.column().classes('gap-2.5 min-w-0 flex-grow'):
                            with ui.row().classes('items-center gap-2 flex-wrap'):
                                ui.label(sub.get('name') or '未命名订阅') \
                                    .classes('text-[15px] font-black tracking-wide leading-none') \
                                    .style('color:var(--xf-text-strong);')
                                tag('组合' if is_collection else '普通',
                                    icon='layers' if is_collection else 'rss_feed', on=True,
                                    tip='节点来自多个成员订阅的合并' if is_collection else '手动勾选的节点')

                            # 指标合并成一条读数带。上一版是 3~5 枚各带底色的彩色药丸，
                            # 权重全都一样，真正要注意的「失效」反而不显眼。
                            metrics = []
                            if is_collection:
                                metrics.append(('成员', len(members), None,
                                                '、'.join(member_names) or '还没有成员'))
                            else:
                                metrics.append(('节点', f'{valid_count}/{total_count}',
                                                None if valid_count else 'warn', '有效 / 已勾选'))

                            if delivered is not None:
                                # 有效数 ≠ 实际下发数，说明筛选规则把节点刷掉了。不点开预览也能看见，
                                # 免得「明明选了 20 个客户端只有 3 个」还得去猜。
                                mismatch = (not is_collection) and delivered != valid_count
                                metrics.append((
                                    '下发', delivered,
                                    'warn' if (mismatch or not delivered) else None,
                                    '按筛选 / 改名规则算完后真正给客户端的节点数'
                                    + ('（筛选规则刷掉了一部分）' if mismatch else '')))

                            if dead_keys:
                                metrics.append(('失效', len(dead_keys), 'danger',
                                                '节点所在服务器已被删除，或 x-ui 里的 ID 变了'))
                            if broken_members:
                                metrics.append(('成员失效', broken_members, 'danger',
                                                '引用的成员订阅已被删除'))

                            acc = access_text(token)
                            if acc:
                                metrics.append(('拉取', acc.replace('拉取 ', '', 1), None,
                                                '访问统计只放内存，面板重启后清零'))

                            inline_readout(metrics, is_dark)

                            if is_collection and member_names:
                                ui.label('合并: ' + ' + '.join(member_names)) \
                                    .classes('text-[10px] font-mono') \
                                    .style('color:var(--xf-text-subtle);')

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

                                act(f'清理失效 {len(dead_keys)}', 'cleaning_services', do_clean,
                                    '剔除指向已删服务器 / 已变 ID 的节点', 'warn', is_dark)

                            act('管理', 'tune', lambda s=sub: open_advanced_sub_editor(s),
                                '选节点 / 筛地区 / 重命名 / 预览', is_dark=is_dark)

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

                            icon_btn('delete', do_del, '删除这条订阅', 'danger', is_dark)

                    def format_buttons():
                        icon_btn('content_copy', lambda u=raw_url: safe_copy_to_clipboard(u),
                                 '复制原始链接（按客户端 UA 自动返回对应格式）', None, is_dark, 'xs')
                        icon_btn('bolt', lambda u=f"{origin}/get/sub/surge/{token}": safe_copy_to_clipboard(u),
                                 '复制 Surge 订阅', 'warn', is_dark, 'xs')
                        icon_btn('cloud_queue', lambda u=f"{origin}/get/sub/clash/{token}": safe_copy_to_clipboard(u),
                                 '复制 Clash 订阅', 'ok', is_dark, 'xs')
                        icon_btn('qr_code_2',
                                 lambda n=sub.get('name', '订阅'), p=sub_url_pairs(origin, token):
                                     open_qr_dialog(n, p),
                                 '扫码导入（可切换格式）', None, is_dark, 'xs')

                        more = ui.button(icon='more_horiz').props('flat dense round size=xs') \
                            .style('color:var(--xf-text-muted);')
                        more.tooltip('全部输出格式')
                        with more:
                            # 底色 / 边框不用管：main_page 里有一条全局 .q-menu 规则拿
                            # !important 设了 --xf-popup-*，这里再写 inline 也是输
                            with ui.menu().props('auto-close'):
                                for t in ordered_targets():
                                    url = f"{origin}/get/sub/{t}/{token}"
                                    ui.menu_item(f"复制 {SUB_TARGETS[t]} 链接",
                                                 on_click=lambda u=url: safe_copy_to_clipboard(u)) \
                                        .classes('text-xs')
                                ui.separator()
                                ui.menu_item('复制原始链接（自适应）',
                                             on_click=lambda u=raw_url: safe_copy_to_clipboard(u)) \
                                    .classes('text-xs')

                    link_bar(raw_url, format_buttons, '点击复制原始链接（客户端自适应）')

            if not SUBS_CACHE:
                empty_row('rss_feed', '还没有订阅',
                          '从上面的节点池里勾几个节点建一条订阅，客户端填上链接就能用',
                          '新建订阅', 'add', lambda: open_advanced_sub_editor(None), is_dark)
            else:
                group_label('rss_feed', '普通订阅', len(normal_subs), '手动勾选节点')
                if not normal_subs:
                    thin_hint('暂无普通订阅。普通订阅是直接勾选节点的那种，组合订阅的成员必须是它。')
                with ui.column().classes('w-full gap-2 mb-2'):
                    for idx, sub in normal_subs:
                        render_sub_card(idx, sub)

                group_label('layers', '组合订阅', len(collections), '合并多条订阅的节点')
                if not collections:
                    thin_hint('暂无组合订阅。在「新建订阅」里打开组合模式，就能把几条订阅合并成一条链接下发。')
                with ui.column().classes('w-full gap-2'):
                    for idx, sub in collections:
                        render_sub_card(idx, sub)
