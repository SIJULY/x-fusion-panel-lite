import asyncio
import re
import secrets
import uuid

from nicegui import app, ui

from app.core.state import NODES_DATA, SERVERS_CACHE, SUBS_CACHE, INDEPENDENT_NODES_CACHE
from app.services.sub_pipeline import node_region, resolve_sub_nodes
from app.storage.repositories import save_subs, save_independent_nodes
from app.ui.common.notifications import safe_notify
from app.ui.pages.subs_page import load_subs_view
from app.utils.geo import detect_country_group

# 批量导入认的协议。写死一份白名单而不是「有 :// 就收」——粘错内容（网页 URL、
# 半截 base64）当场报出来，比等到客户端导入失败再回头查要省事得多。
SUPPORTED_SCHEMES = (
    'vmess', 'vless', 'trojan', 'ss', 'ssr',
    'hy2', 'hysteria', 'hysteria2', 'tuic', 'snell', 'socks', 'socks5', 'http', 'https',
)


def new_token():
    """新订阅的 token。

    订阅链接是公网可访问且**无鉴权**的，token 就是唯一凭据，所以要够长够随机。
    历史上这里混用了 uuid4() 全长和 uuid4()[:8]（只有 32 bit 熵，可爆破）。
    已存在的 token 一律不动——改了用户已经导入到客户端里的链接就全失效了。
    """
    return secrets.token_urlsafe(16)


def parse_import_line(line):
    """解析一行分享链接，返回 (节点 dict, 错误说明)，只有一个非空。"""
    from urllib.parse import unquote, urlparse

    raw = str(line or '').strip()
    if not raw or raw.startswith('#') or raw.startswith('//'):
        return None, None  # 空行和注释：静默跳过，不算失败

    if '://' not in raw:
        return None, '不是分享链接'

    scheme = raw.split('://', 1)[0].lower()
    if scheme not in SUPPORTED_SCHEMES:
        return None, f'不支持的协议 {scheme}'

    remark = ''
    if scheme == 'vmess':
        # vmess 的名字在 base64 载荷的 ps 字段里，不在 fragment 上
        try:
            import json

            from app.utils.encoding import decode_base64_safe

            payload = json.loads(decode_base64_safe(raw[len('vmess://'):]))
            remark = str(payload.get('ps') or payload.get('remark') or '').strip()
        except Exception:
            return None, 'vmess 载荷无法解析'
    else:
        if '#' in raw:
            remark = unquote(raw.split('#', 1)[-1]).strip()
        if not remark:
            try:
                parsed = urlparse(raw)
                if parsed.hostname:
                    remark = f"{parsed.hostname}:{parsed.port or ''}".rstrip(':')
            except Exception:
                pass

    if not remark:
        remark = f'{scheme} 节点'

    return {
        'id': f'indep_{uuid.uuid4().hex[:8]}',
        'remark': remark,
        '_raw_link': raw,
        'enable': True,
    }, None


class IndependentNodeEditor:
    def __init__(self, data=None):
        self.data = data
        if data:
            self.d = data.copy()
        else:
            self.d = {'id': f'indep_{uuid.uuid4().hex[:8]}', 'remark': '', '_raw_link': '', 'enable': True}

        self.remark_input = None
        self.link_input = None

    def ui(self, dlg):
        is_dark = bool(app.storage.user.get('is_dark', True))
        with ui.card().classes('w-[90vw] max-w-lg p-0 overflow-hidden rounded-sm bg-[#070b14] border border-[#1e3a5f]/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-[90vw] max-w-lg p-0 overflow-hidden rounded-sm bg-white border border-slate-300/90 shadow-[0_10px_28px_rgba(148,163,184,0.18)]'):
            with ui.row().classes('w-full justify-between items-center p-4 border-b border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14]' if is_dark else 'w-full justify-between items-center p-4 border-b border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff]'):
                ui.label('独立节点编辑器').classes('text-xl font-black text-slate-100 tracking-wide' if is_dark else 'text-xl font-black text-slate-800 tracking-wide')
                ui.button(icon='close', on_click=dlg.close).props('flat round dense').classes('text-slate-400 hover:text-cyan-300 hover:bg-cyan-950/30' if is_dark else 'text-slate-500 hover:text-sky-700 hover:bg-sky-100')

            with ui.column().classes('w-full p-4 gap-4 bg-[#030712]' if is_dark else 'w-full p-4 gap-4 bg-[#f8fbff]'):
                self.remark_input = ui.input('节点名称', value=self.d.get('remark', '')).classes('w-full').props('outlined dark color=cyan standout bg-color="[#050b14]"' if is_dark else 'outlined color=blue')

                self.link_input = ui.textarea('节点分享链接', value=self.d.get('_raw_link', '')).classes('w-full').props('outlined dark color=cyan standout bg-color="[#050b14]" rows=4' if is_dark else 'outlined color=blue rows=4')
                ui.label('支持 vmess://, vless://, trojan://, ss://, hy2:// 等各种分享链接').classes('text-xs text-slate-500 mt-[-10px]')

            with ui.row().classes('w-full p-4 border-t border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14]' if is_dark else 'w-full p-4 border-t border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff]'):
                async def save():
                    remark = self.remark_input.value.strip()
                    link = self.link_input.value.strip()

                    if not remark:
                        return safe_notify("节点名称不能为空", "negative")
                    if not link:
                        return safe_notify("节点链接不能为空", "negative")

                    # 校验但不拦：白名单之外的协议只提醒，用户可能真有特殊用法
                    scheme = link.split('://', 1)[0].lower() if '://' in link else ''
                    if scheme not in SUPPORTED_SCHEMES:
                        safe_notify(f'⚠️ 协议 "{scheme or "无"}" 不在已知列表内，已按原样保存', 'warning')

                    self.d['remark'] = remark
                    self.d['_raw_link'] = link

                    if self.data:
                        try:
                            idx = INDEPENDENT_NODES_CACHE.index(self.data)
                            INDEPENDENT_NODES_CACHE[idx] = self.d
                        except ValueError:
                            INDEPENDENT_NODES_CACHE.append(self.d)
                    else:
                        INDEPENDENT_NODES_CACHE.append(self.d)

                    await save_independent_nodes()
                    await load_subs_view()
                    dlg.close()
                    ui.notify('独立节点保存成功', color='positive')

                ui.button('保存', icon='save', on_click=save).props('flat').classes('w-full h-12 bg-cyan-950/45 text-cyan-300 border border-cyan-500/45 hover:bg-cyan-900/55 font-black rounded-sm' if is_dark else 'w-full h-12 bg-sky-100 text-sky-700 border border-sky-300 hover:bg-sky-200 font-black rounded-sm')


def open_independent_node_editor(data=None):
    with ui.dialog() as dlg:
        IndependentNodeEditor(data).ui(dlg)
        dlg.open()


def open_batch_import_dialog():
    """批量粘贴导入独立节点，一行一条。"""
    is_dark = bool(app.storage.user.get('is_dark', True))

    with ui.dialog() as dlg, ui.card().classes('w-[90vw] max-w-2xl p-0 overflow-hidden rounded-sm bg-[#070b14] border border-[#1e3a5f]/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-[90vw] max-w-2xl p-0 overflow-hidden rounded-sm bg-white border border-slate-300/90 shadow-[0_10px_28px_rgba(148,163,184,0.18)]'):
        with ui.row().classes('w-full justify-between items-center p-4 border-b border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14]' if is_dark else 'w-full justify-between items-center p-4 border-b border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff]'):
            ui.label('批量导入独立节点').classes('text-xl font-black text-slate-100 tracking-wide' if is_dark else 'text-xl font-black text-slate-800 tracking-wide')
            ui.button(icon='close', on_click=dlg.close).props('flat round dense').classes('text-slate-400 hover:text-cyan-300 hover:bg-cyan-950/30' if is_dark else 'text-slate-500 hover:text-sky-700 hover:bg-sky-100')

        with ui.column().classes('w-full p-4 gap-3 bg-[#030712]' if is_dark else 'w-full p-4 gap-3 bg-[#f8fbff]'):
            box = ui.textarea('每行一条分享链接', placeholder='vmess://...\nvless://...\ntrojan://...') \
                .classes('w-full font-mono') \
                .props('outlined dark color=cyan standout bg-color="[#050b14]" rows=12' if is_dark else 'outlined color=blue rows=12')
            ui.label('名称自动取自链接备注（vmess 取载荷里的 ps）；空行与 # 开头的行会跳过。') \
                .classes('text-xs text-slate-500')
            result_box = ui.column().classes('w-full gap-1')

        with ui.row().classes('w-full p-4 border-t border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14] gap-2 justify-end' if is_dark else 'w-full p-4 border-t border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff] gap-2 justify-end'):
            async def do_import():
                lines = str(box.value or '').splitlines()
                existing_links = {str(n.get('_raw_link', '')).strip() for n in INDEPENDENT_NODES_CACHE}

                added, skipped, dup = [], [], 0
                for i, line in enumerate(lines, 1):
                    node, err = parse_import_line(line)
                    if err:
                        skipped.append(f'第 {i} 行：{err}')
                        continue
                    if not node:
                        continue
                    if node['_raw_link'] in existing_links:
                        dup += 1
                        continue
                    existing_links.add(node['_raw_link'])
                    added.append(node)

                result_box.clear()
                with result_box:
                    if not added and not skipped and not dup:
                        ui.label('没有可导入的内容').classes('text-xs text-amber-400')
                        return
                    ui.label(f'✅ 成功 {len(added)} 条'
                             + (f'，跳过重复 {dup} 条' if dup else '')
                             + (f'，失败 {len(skipped)} 条' if skipped else '')) \
                        .classes('text-xs font-bold text-emerald-400')
                    for msg in skipped[:12]:
                        ui.label(f'· {msg}').classes('text-[11px] text-rose-400 font-mono')
                    if len(skipped) > 12:
                        ui.label(f'· 还有 {len(skipped) - 12} 条失败未列出').classes('text-[11px] text-rose-400')

                if not added:
                    return

                INDEPENDENT_NODES_CACHE.extend(added)
                await save_independent_nodes()
                await load_subs_view()
                safe_notify(f'已导入 {len(added)} 个独立节点', 'positive')
                dlg.close()

            ui.button('取消', on_click=dlg.close).props('outline color=grey').classes('text-slate-300 border-slate-600 rounded-sm' if is_dark else 'text-slate-600 border-slate-300 rounded-sm')
            ui.button('开始导入', icon='upload', on_click=do_import).props('flat').classes('bg-cyan-950/45 text-cyan-300 border border-cyan-500/45 hover:bg-cyan-900/55 font-black rounded-sm px-5' if is_dark else 'bg-sky-100 text-sky-700 border border-sky-300 hover:bg-sky-200 font-black rounded-sm px-5')

        dlg.open()


class AdvancedSubEditor:
    def __init__(self, sub_data=None):
        import copy
        if sub_data:
            self.sub = copy.deepcopy(sub_data)
        else:
            self.sub = {'name': '', 'token': new_token(), 'nodes': [], 'options': {}}

        if 'options' not in self.sub:
            self.sub['options'] = {}

        # B2 修复的关键：用**进编辑器时的原始 token** 定位记录。
        # 原来直接拿 self.sub['token'] 去 SUBS_CACHE 里找，用户一改 token 就匹配不到
        # 原记录，于是走 append —— 同一条订阅变成两条。
        self.original_token = self.sub.get('token') if sub_data else None

        self.selected_ids = list(self.sub.get('nodes', []))
        self.members = list(self.sub.get('members', []) or [])
        self.is_collection = self.sub.get('type') == 'collection'

        self.all_nodes_map = {}
        self.node_servers = {}
        self.ui_groups = {}
        self.server_expansions = {}
        self.server_items = {}
        self.search_text = ""
        self.preview_container = None
        self.left_scroll = None
        self.list_container = None
        self.token_input = None
        self.collection_hint = None
        self.member_section = None

    # ---------- 选项读写 ----------

    def opt(self, key, default=None):
        return self.sub.get('options', {}).get(key, default)

    def set_opt(self, key, value):
        self.sub.setdefault('options', {})[key] = value

    def current_sub_snapshot(self):
        """按编辑器当前（未保存）状态拼一份订阅 dict，供预览用。"""
        snap = dict(self.sub)
        snap['nodes'] = list(self.selected_ids)
        snap['options'] = dict(self.sub.get('options', {}))
        if self.is_collection:
            snap['type'] = 'collection'
            snap['members'] = list(self.members)
        else:
            snap.pop('type', None)
            snap.pop('members', None)
        return snap

    def available_regions(self):
        """节点里实际出现过的地区 + 已选中的地区（哪怕现在没节点也别悄悄丢掉）。"""
        found = set()
        for key, node in self.all_nodes_map.items():
            try:
                found.add(node_region(node, self.node_servers.get(key)))
            except Exception:
                pass
        found.update(self.opt('regions', []) or [])
        return sorted(found)

    def ui(self, dlg):
        self._preload_data()

        is_dark = bool(app.storage.user.get('is_dark', True))
        self.is_dark = is_dark
        label_cls = 'text-xs font-bold text-blue-400 uppercase' if is_dark else 'text-xs font-bold text-sky-700 uppercase'
        inp_props = 'outlined dense dark color=cyan standout bg-color="[#050b14]"' if is_dark else 'outlined dense color=blue'
        chk_props = 'dense size=sm dark color=cyan' if is_dark else 'dense size=sm color=blue'
        sub_btn_cls = ('w-full bg-cyan-950/45 text-cyan-300 border border-cyan-500/45 hover:bg-cyan-900/55'
                       if is_dark else 'w-full bg-sky-100 text-sky-700 border border-sky-300 hover:bg-sky-200')

        with ui.card().classes('w-full max-w-6xl h-[90vh] flex flex-col p-0 overflow-hidden rounded-sm bg-[#070b14] border border-[#1e3a5f]/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-full max-w-6xl h-[90vh] flex flex-col p-0 overflow-hidden rounded-sm bg-white border border-slate-300/90 shadow-[0_10px_28px_rgba(148,163,184,0.18)]'):
            with ui.row().classes('w-full p-4 border-b border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14] justify-between items-center flex-shrink-0' if is_dark else 'w-full p-4 border-b border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff] justify-between items-center flex-shrink-0'):
                with ui.row().classes('items-center gap-3'):
                    with ui.element('div').classes('w-9 h-9 rounded-sm flex items-center justify-center bg-[#050b14] border border-[#1e3a5f] text-cyan-400 shadow-[0_0_8px_rgba(0,0,0,0.7)] relative overflow-hidden' if is_dark else 'w-9 h-9 rounded-sm flex items-center justify-center bg-sky-50 border border-slate-300 text-sky-600 shadow-[0_4px_12px_rgba(148,163,184,0.14)] relative overflow-hidden'):
                        ui.element('div').classes('absolute inset-0 bg-cyan-400/10')
                        ui.icon('tune').classes('text-[18px] drop-shadow-[0_0_5px_currentColor]')
                    ui.label('订阅高级管理').classes('text-lg font-black text-slate-100 tracking-wide' if is_dark else 'text-lg font-black text-slate-800 tracking-wide')
                with ui.row().classes('items-center gap-2'):
                    ui.button('预览下发结果', icon='visibility', on_click=self.open_preview) \
                        .props('flat dense size=sm') \
                        .classes('text-cyan-300 border border-cyan-500/45 rounded-sm px-3 font-bold' if is_dark else 'text-sky-700 border border-sky-300 rounded-sm px-3 font-bold') \
                        .tooltip('按当前设置算一遍：每级筛选刷掉了哪些节点、最终叫什么名字')
                    ui.button(icon='close', on_click=dlg.close).props('flat round dense color=grey').classes('text-slate-400 hover:text-cyan-300 hover:bg-cyan-950/30' if is_dark else 'text-slate-500 hover:text-sky-700 hover:bg-sky-100')

            with ui.row().classes('w-full flex-grow overflow-hidden gap-0'):
                # ---------- 左：源节点树 ----------
                with ui.column().classes('w-2/5 h-full border-r border-[#1e3a5f]/45 flex flex-col bg-[#030712]' if is_dark else 'w-2/5 h-full border-r border-slate-300/90 flex flex-col bg-[#f8fbff]'):
                    with ui.column().classes('w-full p-2 border-b border-[#1e3a5f]/45 bg-[#070b14] gap-2' if is_dark else 'w-full p-2 border-b border-slate-300/90 bg-white gap-2'):
                        ui.input(placeholder='🔍 搜索源节点...', on_change=self.on_search).props('outlined dense dark color=cyan standout debounce="300" bg-color="[#050b14]"' if is_dark else 'outlined dense color=blue debounce="300"').classes('w-full')

                        with ui.row().classes('w-full justify-between items-center'):
                            ui.label('筛选结果操作:').classes('text-xs text-slate-500')
                            with ui.row().classes('gap-1'):
                                ui.button('全选', icon='add_circle', on_click=lambda: self.batch_select(True)) \
                                    .props('unelevated dense size=sm color=blue-7').tooltip('将搜索结果加入右侧')

                                ui.button('清空', icon='remove_circle', on_click=lambda: self.batch_select(False)) \
                                    .props('flat dense size=sm color=grey-6').tooltip('从右侧移除搜索结果')

                    self.collection_hint = ui.row().classes('w-full px-3 py-2 items-center gap-2 bg-amber-950/30 border-b border-amber-700/40' if is_dark else 'w-full px-3 py-2 items-center gap-2 bg-amber-50 border-b border-amber-300')
                    with self.collection_hint:
                        ui.icon('info', size='xs').classes('text-amber-400')
                        ui.label('组合订阅的节点来自成员订阅，这里手选的节点不会下发').classes('text-[11px] text-amber-300 font-bold' if is_dark else 'text-[11px] text-amber-700 font-bold')
                    self.collection_hint.set_visibility(self.is_collection)

                    with ui.scroll_area().classes('w-full flex-grow p-2') as area:
                        self.left_scroll = area
                        self.list_container = ui.column().classes('w-full gap-2')
                        ui.timer(0.1, lambda: asyncio.create_task(self._render_node_tree()), once=True)

                # ---------- 中：设置 ----------
                with ui.column().classes('w-1/4 h-full border-r border-slate-700 flex flex-col bg-[#1e293b] overflow-y-auto' if is_dark else 'w-1/4 h-full border-r border-slate-300 flex flex-col bg-white overflow-y-auto'):
                    with ui.column().classes('w-full p-4 gap-4'):
                        ui.label('① 基础设置').classes(label_cls)
                        ui.input('订阅名称', value=self.sub.get('name', '')) \
                            .bind_value_to(self.sub, 'name') \
                            .props(inp_props).classes('w-full')

                        with ui.row().classes('w-full gap-1'):
                            self.token_input = ui.input('Token', value=self.sub.get('token', '')) \
                                .bind_value_to(self.sub, 'token') \
                                .props(inp_props).classes('flex-grow')

                            # B1 修复：bind_value_to 是**单向**的（元素 → dict），
                            # 光改 dict 不会反向刷新输入框，用户点了刷新看不到任何变化，
                            # 却以为没生效。必须显式 set_value 回写元素。
                            ui.button(icon='refresh', on_click=self.regen_token) \
                                .props('flat dense color=blue') \
                                .classes('text-cyan-400 hover:bg-cyan-950/30 hover:text-cyan-300' if is_dark else 'text-sky-600 hover:bg-sky-100 hover:text-sky-700') \
                                .tooltip('重新生成 Token（旧链接立即失效）')

                        ui.checkbox('组合订阅（合并其它订阅的节点）', value=self.is_collection,
                                    on_change=self.on_type_change).props(chk_props).classes('text-xs')

                        self.member_section = ui.column().classes('w-full gap-1 bg-[#0f172a] p-2 rounded border border-slate-700' if is_dark else 'w-full gap-1 bg-sky-50 p-2 rounded border border-slate-300')
                        with self.member_section:
                            ui.label('成员订阅').classes('text-[11px] font-bold text-slate-400')
                            others = [s for s in SUBS_CACHE if s.get('token') != self.original_token]
                            if not others:
                                ui.label('还没有其它订阅可以合并').classes('text-[11px] text-slate-500')
                            for s in others:
                                tok = s.get('token')
                                ui.checkbox(s.get('name') or '未命名', value=tok in self.members,
                                            on_change=lambda e, t=tok: self.toggle_member(t, e.value)) \
                                    .props(chk_props).classes('text-xs')
                        self.member_section.set_visibility(self.is_collection)

                        ui.separator().classes('bg-slate-700' if is_dark else 'bg-slate-300')

                        ui.label('② 排序工具').classes(label_cls)
                        with ui.grid().classes('w-full grid-cols-2 gap-2'):
                            ui.button('名称 A-Z', on_click=lambda: self.sort_nodes('name_asc')).props('outline dense size=sm color=slate-400').classes('text-slate-300 border-slate-600 hover:bg-slate-800/40' if is_dark else 'text-slate-600 border-slate-300 hover:bg-slate-100')
                            ui.button('名称 Z-A', on_click=lambda: self.sort_nodes('name_desc')).props('outline dense size=sm color=slate-400').classes('text-slate-300 border-slate-600 hover:bg-slate-800/40' if is_dark else 'text-slate-600 border-slate-300 hover:bg-slate-100')
                            ui.button('随机打乱', on_click=lambda: self.sort_nodes('random')).props('outline dense size=sm color=slate-400').classes('text-slate-300 border-slate-600 hover:bg-slate-800/40' if is_dark else 'text-slate-600 border-slate-300 hover:bg-slate-100')
                            ui.button('列表倒序', on_click=lambda: self.sort_nodes('reverse')).props('outline dense size=sm color=slate-400').classes('text-slate-300 border-slate-600 hover:bg-slate-800/40' if is_dark else 'text-slate-600 border-slate-300 hover:bg-slate-100')

                        ui.separator().classes('bg-slate-700' if is_dark else 'bg-slate-300')

                        ui.label('③ 节点筛选').classes(label_cls)
                        with ui.column().classes('w-full gap-2 bg-[#0f172a] p-2 rounded border border-slate-700' if is_dark else 'w-full gap-2 bg-sky-50 p-2 rounded border border-slate-300'):
                            ui.select(self.available_regions(), multiple=True, label='只保留这些地区',
                                      value=list(self.opt('regions', []) or []),
                                      on_change=lambda e: self.set_opt('regions', list(e.value or []))) \
                                .props(inp_props + ' use-chips clearable').classes('w-full')
                            ui.input('名称必须匹配（正则）', value=self.opt('include_regex', '') or '',
                                     on_change=lambda e: self.set_opt('include_regex', e.value)) \
                                .props(inp_props).classes('w-full')
                            ui.input('名称匹配则排除（正则）', value=self.opt('exclude_regex', '') or '',
                                     on_change=lambda e: self.set_opt('exclude_regex', e.value)) \
                                .props(inp_props).classes('w-full')
                            ui.checkbox('包含已在 x-ui 里禁用的节点', value=bool(self.opt('include_disabled', False)),
                                        on_change=lambda e: self.set_opt('include_disabled', bool(e.value))) \
                                .props(chk_props).classes('text-xs') \
                                .tooltip('默认不下发禁用节点，否则客户端会拿到一个连不上的节点')

                        ui.separator().classes('bg-slate-700' if is_dark else 'bg-slate-300')

                        ui.label('④ 批量重命名').classes(label_cls)
                        with ui.column().classes('w-full gap-2 bg-[#0f172a] p-2 rounded border border-slate-700' if is_dark else 'w-full gap-2 bg-sky-50 p-2 rounded border border-slate-300'):
                            pat = ui.input('正则 (如: ^)', value=self.opt('rename_pattern', '') or '').props(inp_props).classes('w-full')
                            rep = ui.input('替换 (如: VIP-)', value=self.opt('rename_replacement', '') or '').props(inp_props).classes('w-full')
                            ui.checkbox('自动补地区旗帜', value=bool(self.opt('add_flag', False)),
                                        on_change=lambda e: (self.set_opt('add_flag', bool(e.value)), self.update_preview())) \
                                .props(chk_props).classes('text-xs') \
                                .tooltip('名称已有旗帜的不会重复添加')

                            def apply_regex():
                                self.set_opt('rename_pattern', pat.value)
                                self.set_opt('rename_replacement', rep.value)
                                try:
                                    re.compile(pat.value or '')
                                except re.error as err:
                                    return safe_notify(f'正则无效：{err}', 'negative')
                                self.update_preview()
                                safe_notify('预览已刷新', 'positive')

                            ui.button('刷新预览', on_click=apply_regex).props('unelevated dense size=sm color=blue').classes(sub_btn_cls)

                        ui.separator().classes('bg-slate-700' if is_dark else 'bg-slate-300')

                        ui.label('⑤ 客户端选项').classes(label_cls)
                        with ui.column().classes('w-full gap-1 bg-[#0f172a] p-2 rounded border border-slate-700' if is_dark else 'w-full gap-1 bg-sky-50 p-2 rounded border border-slate-300'):
                            ui.label('仅对 Clash / sing-box 等转换格式生效').classes('text-[10px] text-slate-500')
                            ui.checkbox('开启 UDP 转发', value=bool(self.opt('udp', True)),
                                        on_change=lambda e: self.set_opt('udp', bool(e.value))).props(chk_props).classes('text-xs')
                            ui.checkbox('开启 TCP Fast Open', value=bool(self.opt('tfo', False)),
                                        on_change=lambda e: self.set_opt('tfo', bool(e.value))).props(chk_props).classes('text-xs')
                            ui.checkbox('跳过证书验证', value=bool(self.opt('skip_cert', True)),
                                        on_change=lambda e: self.set_opt('skip_cert', bool(e.value))).props(chk_props).classes('text-xs')
                            ui.checkbox('由转换器添加 Emoji', value=bool(self.opt('emoji', False)),
                                        on_change=lambda e: self.set_opt('emoji', bool(e.value))).props(chk_props).classes('text-xs') \
                                .tooltip('和上面的「自动补地区旗帜」二选一，同时开会加两个旗帜')

                # ---------- 右：已选清单 ----------
                with ui.column().classes('w-[35%] h-full bg-[#0f172a] flex flex-col' if is_dark else 'w-[35%] h-full bg-slate-50 flex flex-col'):
                    with ui.row().classes('w-full p-3 border-b border-slate-700 bg-[#1e293b] items-center justify-between shadow-sm z-10' if is_dark else 'w-full p-3 border-b border-slate-300 bg-white items-center justify-between shadow-sm z-10'):
                        ui.label('已选节点清单').classes('font-bold text-slate-200' if is_dark else 'font-bold text-slate-800')
                        with ui.row().classes('items-center gap-2'):
                            ui.label('').bind_text_from(self, 'selected_ids', lambda x: f"{len(x)}").classes('text-slate-400')
                            ui.button('清空全部', icon='delete_forever', on_click=self.clear_all_selected).props('flat dense size=sm color=red')

                    with ui.scroll_area().classes('w-full flex-grow p-2'):
                        self.preview_container = ui.column().classes('w-full gap-1')
                        self.update_preview()

            with ui.row().classes('w-full p-3 border-t border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14] justify-end gap-3 flex-shrink-0' if is_dark else 'w-full p-3 border-t border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff] justify-end gap-3 flex-shrink-0'):
                async def save_all():
                    ok, err = self.validate_and_commit()
                    if not ok:
                        return safe_notify(err, 'negative')

                    await save_subs()
                    await load_subs_view()
                    dlg.close()
                    safe_notify('✅ 订阅保存成功', 'positive')

                ui.button('保存配置', icon='save', on_click=save_all).props('flat').classes('bg-cyan-950/45 text-cyan-300 border border-cyan-500/45 hover:bg-cyan-900/55 font-black rounded-sm px-5' if is_dark else 'bg-sky-100 text-sky-700 border border-sky-300 hover:bg-sky-200 font-black rounded-sm px-5')

    # ---------- 校验与落盘 ----------

    def validate_and_commit(self):
        """校验并把编辑结果写回 SUBS_CACHE。返回 (成功?, 错误说明)。

        独立成一个纯函数（不碰 UI、不落盘），B1/B2 这类回归才好直接测。
        """
        name = str(self.sub.get('name') or '').strip()
        if not name:
            return False, '名称不能为空'
        self.sub['name'] = name

        token = str(self.sub.get('token') or '').strip()
        if not token:
            return False, 'Token 不能为空'
        if any(c in token for c in ('/', '?', '#', ' ', '&')):
            return False, 'Token 不能含空格或 / ? # & 等 URL 特殊字符'
        self.sub['token'] = token

        # 新 token 撞上别人的会让两条订阅共用一条链接，必须拦住
        for s in SUBS_CACHE:
            if s.get('token') == token and s.get('token') != self.original_token:
                return False, f'Token 与订阅「{s.get("name")}」重复'

        self.sub['nodes'] = list(self.selected_ids)

        if self.is_collection:
            if not self.members:
                return False, '组合订阅至少要选一个成员订阅'
            if self.original_token and self.original_token in self.members:
                return False, '组合订阅不能把自己当成员'
            self.sub['type'] = 'collection'
            self.sub['members'] = list(self.members)
        else:
            # 缺失 type 即普通订阅，所以直接删掉字段，老记录零迁移
            self.sub.pop('type', None)
            self.sub.pop('members', None)

        # B2 修复：用**进编辑器时的原始 token** 定位记录，而不是可能刚被改过的当前 token。
        # 原来用当前 token 匹配，用户一改 token 就找不到原记录 → 走 append → 变成两条。
        target = self.original_token if self.original_token is not None else token
        for i, s in enumerate(SUBS_CACHE):
            if s.get('token') == target:
                SUBS_CACHE[i] = self.sub
                break
        else:
            SUBS_CACHE.append(self.sub)

        # 改完 token 后原始 token 也要跟上，否则同一个弹窗里连按两次保存又会新增一条
        self.original_token = token
        return True, None

    # ---------- 交互 ----------

    def regen_token(self):
        token = new_token()
        self.sub['token'] = token
        if self.token_input is not None:
            self.token_input.set_value(token)
        safe_notify('已生成新 Token，保存后旧链接失效', 'warning')

    def on_type_change(self, e):
        self.is_collection = bool(e.value)
        if self.collection_hint is not None:
            self.collection_hint.set_visibility(self.is_collection)
        if self.member_section is not None:
            self.member_section.set_visibility(self.is_collection)

    def toggle_member(self, token, checked):
        if checked:
            if token not in self.members:
                self.members.append(token)
        elif token in self.members:
            self.members.remove(token)

    def open_preview(self):
        """跑一遍真实管线，把每级筛选刷掉多少、最终名字是什么摊开给用户看。

        排查「为什么这个节点没出现在客户端里」时，这里是唯一一个能直接给出答案的地方。
        """
        snap = self.current_sub_snapshot()
        try:
            resolved, stats = resolve_sub_nodes(snap, collect_stats=True)
        except Exception as err:
            return safe_notify(f'预览失败: {err}', 'negative')

        is_dark = getattr(self, 'is_dark', True)
        rows = [
            ('引用节点', stats['referenced'], 'text-slate-300' if is_dark else 'text-slate-700'),
            ('失效（服务器已删/ID 变了）', stats['missing'], 'text-rose-400'),
            ('被「已禁用」刷掉', stats['dropped_disabled'], 'text-amber-400'),
            ('被地区筛选刷掉', stats['dropped_region'], 'text-amber-400'),
            ('未匹配「必须匹配」', stats['dropped_include'], 'text-amber-400'),
            ('命中「排除」', stats['dropped_exclude'], 'text-amber-400'),
            ('最终下发', stats['kept'], 'text-emerald-400'),
        ]

        with ui.dialog() as d, ui.card().classes('w-[90vw] max-w-3xl p-0 overflow-hidden rounded-sm bg-[#070b14] border border-[#1e3a5f]/55' if is_dark else 'w-[90vw] max-w-3xl p-0 overflow-hidden rounded-sm bg-white border border-slate-300/90'):
            with ui.row().classes('w-full justify-between items-center p-4 border-b border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14]' if is_dark else 'w-full justify-between items-center p-4 border-b border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff]'):
                ui.label('下发结果预览（按当前未保存的设置计算）').classes('text-base font-black text-slate-100' if is_dark else 'text-base font-black text-slate-800')
                ui.button(icon='close', on_click=d.close).props('flat round dense color=grey')

            with ui.column().classes('w-full p-4 gap-3 bg-[#030712] max-h-[70vh] overflow-y-auto' if is_dark else 'w-full p-4 gap-3 bg-[#f8fbff] max-h-[70vh] overflow-y-auto'):
                if self.is_collection:
                    ui.label(f'组合订阅，合并 {len(self.members)} 个成员订阅的节点').classes('text-xs text-amber-400 font-bold')

                with ui.column().classes('w-full gap-1 p-3 rounded border border-slate-700 bg-[#0f172a]' if is_dark else 'w-full gap-1 p-3 rounded border border-slate-300 bg-white'):
                    for text, num, color in rows:
                        with ui.row().classes('w-full justify-between items-center'):
                            ui.label(text).classes('text-xs text-slate-400')
                            ui.label(str(num)).classes(f'text-xs font-mono font-bold {color}')

                if resolved:
                    ui.label('最终节点（左：原名 → 右：下发名）').classes('text-xs font-bold text-blue-400 mt-2')
                    with ui.column().classes('w-full gap-0.5'):
                        for i, item in enumerate(resolved, 1):
                            with ui.row().classes('w-full items-center gap-2 px-2 py-1 rounded bg-[#0f172a] border border-slate-800' if is_dark else 'w-full items-center gap-2 px-2 py-1 rounded bg-white border border-slate-200'):
                                ui.label(f'{i}').classes('text-[10px] text-slate-500 w-6 text-right font-mono')
                                ui.label(item['region']).classes('text-[10px] text-slate-500 w-24 truncate')
                                ui.label(item['original_name']).classes('text-[11px] text-slate-500 flex-1 truncate font-mono')
                                ui.icon('arrow_forward', size='xs').classes('text-slate-600')
                                changed = item['final_name'] != item['original_name']
                                ui.label(item['final_name']).classes(
                                    'text-[11px] flex-1 truncate font-mono font-bold '
                                    + ('text-cyan-300' if changed else ('text-slate-300' if is_dark else 'text-slate-700')))
                else:
                    ui.label('按当前设置，一个节点都不会下发——检查上面各级筛选。').classes('text-xs text-rose-400 font-bold')

                if resolved:
                    from app.services.sub_pipeline import build_sub_links, build_userinfo_header

                    info = build_userinfo_header(resolved)
                    ui.label(f"流量与到期头: {info or '（无可用信息，不下发此头）'}").classes('text-[11px] text-slate-500 font-mono mt-2')

                    with ui.expansion('查看最终链接内容', icon='code').classes('w-full text-xs'):
                        ui.code("\n".join(build_sub_links(resolved)) or '（空）').classes('w-full text-[10px]')

            d.open()

    def _preload_data(self):
        self.all_nodes_map = {}
        self.node_servers = {}
        for srv in SERVERS_CACHE:
            nodes = (NODES_DATA.get(srv['url'], []) or []) + srv.get('custom_nodes', [])
            for n in nodes:
                key = f"{srv['url']}|{n['id']}"
                n['_server_name'] = srv['name']
                self.all_nodes_map[key] = n
                self.node_servers[key] = srv

        for inode in INDEPENDENT_NODES_CACHE:
            key = f"independent|{inode['id']}"
            inode['_server_name'] = '独立节点'
            self.all_nodes_map[key] = inode
            self.node_servers[key] = None

    async def _render_node_tree(self):
        self.list_container.clear()
        self.ui_groups = {}
        self.server_expansions = {}
        self.server_items = {}

        grouped = {}
        for srv in SERVERS_CACHE:
            nodes = (NODES_DATA.get(srv['url'], []) or []) + srv.get('custom_nodes', [])
            if not nodes:
                continue

            g_name = srv.get('group', '默认分组')
            if g_name in ['默认分组', '自动注册', '未分组', '自动导入']:
                try:
                    g_name = detect_country_group(srv.get('name'), srv)
                except:
                    pass

            if g_name not in grouped:
                grouped[g_name] = []
            grouped[g_name].append({'server': srv, 'nodes': nodes, 'type': 'server'})

        if INDEPENDENT_NODES_CACHE:
            g_name = '独立节点'
            if g_name not in grouped:
                grouped[g_name] = []
            grouped[g_name].append({'server': {'name': '独立节点库', 'url': 'independent'}, 'nodes': INDEPENDENT_NODES_CACHE, 'type': 'independent'})

        sorted_groups = sorted(grouped.keys())
        with self.list_container:
            for i, g_name in enumerate(sorted_groups):
                if i % 2 == 0:
                    await asyncio.sleep(0.01)

                exp = ui.expansion(g_name, icon='folder', value=True).classes('w-full border border-slate-700 rounded bg-[#1e293b]' if self.is_dark else 'w-full border border-slate-300 rounded bg-white').props('header-class="bg-[#172033] text-slate-300 text-sm font-bold p-2 min-h-0"' if self.is_dark else 'header-class="bg-sky-50 text-slate-700 text-sm font-bold p-2 min-h-0"')

                self.server_expansions[g_name] = exp
                self.server_items[g_name] = []

                with exp:
                    with ui.column().classes('w-full p-2 gap-2'):
                        for item in grouped[g_name]:
                            srv = item['server']
                            search_key = f"{srv['name']}".lower()
                            container = ui.column().classes('w-full gap-1')

                            with container:
                                server_header = ui.row().classes('w-full items-center gap-1 mt-1 px-1')
                                with server_header:
                                    ui.icon('dns', size='xs').classes('text-blue-400' if self.is_dark else 'text-sky-600')
                                    ui.label(srv['name']).classes('text-xs font-bold text-slate-500 truncate' if self.is_dark else 'text-xs font-bold text-slate-700 truncate')

                                for n in item['nodes']:
                                    key = f"{srv['url']}|{n['id']}"
                                    is_checked = key in self.selected_ids
                                    self.server_items[g_name].append(key)

                                    with ui.row().classes('w-full items-center pl-2 py-1 hover:bg-slate-700 rounded cursor-pointer transition border border-transparent' if self.is_dark else 'w-full items-center pl-2 py-1 hover:bg-sky-50 rounded cursor-pointer transition border border-transparent') as row:
                                        chk = ui.checkbox(value=is_checked).props('dense size=xs dark color=blue' if self.is_dark else 'dense size=xs color=blue')
                                        chk.disable()
                                        row.on('click', lambda _, k=key: self.toggle_node_from_left(k))

                                        ui.label(n.get('remark', '未命名')).classes('text-xs text-slate-300 truncate flex-grow' if self.is_dark else 'text-xs text-slate-700 truncate flex-grow')

                                        if not n.get('enable', True):
                                            ui.badge('禁用', color='grey').props('outline size=xs').classes('text-[9px]')

                                        full_text = f"{search_key} {n.get('remark','')} {n.get('protocol','')}".lower()

                                        self.ui_groups[key] = {
                                            'row': row, 'chk': chk, 'text': full_text,
                                            'group_name': g_name, 'header': server_header,
                                            'container': container
                                        }

    def toggle_node_from_left(self, key):
        if key in self.selected_ids:
            self.remove_node(key)
        else:
            self.selected_ids.append(key)
            self.update_preview()
            if key in self.ui_groups:
                self.ui_groups[key]['chk'].value = True
                self.ui_groups[key]['row'].classes(add='bg-blue-900/30 border-blue-500/30', remove='border-transparent')

    def remove_node(self, key):
        if key in self.selected_ids:
            self.selected_ids.remove(key)
            self.update_preview()
            if key in self.ui_groups:
                self.ui_groups[key]['chk'].value = False
                self.ui_groups[key]['row'].classes(remove='bg-blue-900/30 border-blue-500/30', add='border-transparent')

    def clear_all_selected(self):
        for key in list(self.selected_ids):
            self.remove_node(key)

    def update_preview(self):
        self.preview_container.clear()
        pat = self.opt('rename_pattern', '') or ''
        rep = self.opt('rename_replacement', '') or ''
        add_flag = bool(self.opt('add_flag', False))

        with self.preview_container:
            if not self.selected_ids:
                with ui.column().classes('w-full items-center mt-10 text-slate-600 gap-2'):
                    ui.icon('shopping_cart', size='3rem')
                    ui.label('清单为空').classes('text-sm')
                return

            with ui.column().classes('w-full gap-1'):
                for idx, key in enumerate(self.selected_ids):
                    node = self.all_nodes_map.get(key)
                    if not node:
                        continue

                    original_name = node.get('remark', 'Unknown')
                    final_name = original_name
                    if pat:
                        try:
                            final_name = re.sub(pat, rep, original_name)
                        except Exception:
                            pass
                    if add_flag:
                        # 走管线里同一个函数，保证这里显示的和真正下发的一致
                        from app.services.sub_pipeline import apply_flag
                        try:
                            final_name = apply_flag(final_name, node_region(node, self.node_servers.get(key)))
                        except Exception:
                            pass

                    with ui.row().classes('w-full items-center p-1.5 bg-[#1e293b] border border-slate-700 rounded shadow-sm group hover:border-red-500 transition' if self.is_dark else 'w-full items-center p-1.5 bg-white border border-slate-300 rounded shadow-sm group hover:border-rose-300 transition'):
                        ui.label(str(idx + 1)).classes('text-[10px] text-slate-500 w-5 text-center')
                        chk = ui.checkbox(value=True).props('dense size=xs color=green dark' if self.is_dark else 'dense size=xs color=green')
                        chk.on_value_change(lambda e, k=key: self.remove_node(k) if not e.value else None)

                        with ui.column().classes('gap-0 leading-none flex-grow ml-1'):
                            if final_name != original_name:
                                ui.label(final_name).classes('text-xs font-bold text-blue-400' if self.is_dark else 'text-xs font-bold text-sky-700')
                                ui.label(original_name).classes('text-[9px] text-slate-500 line-through')
                            else:
                                ui.label(final_name).classes('text-xs font-bold text-slate-300' if self.is_dark else 'text-xs font-bold text-slate-800')

                        ui.button(icon='close', on_click=lambda _, k=key: self.remove_node(k)).props('flat dense size=xs color=red').classes('opacity-0 group-hover:opacity-100 transition')

    def sort_nodes(self, mode):
        if not self.selected_ids:
            return safe_notify('列表为空', 'warning')
        objs = []
        for k in self.selected_ids:
            n = self.all_nodes_map.get(k)
            if n:
                objs.append({'key': k, 'name': n.get('remark', '').lower()})

        if mode == 'name_asc':
            objs.sort(key=lambda x: x['name'])
        elif mode == 'name_desc':
            objs.sort(key=lambda x: x['name'], reverse=True)
        elif mode == 'random':
            import random
            random.shuffle(objs)
        elif mode == 'reverse':
            objs.reverse()

        self.selected_ids = [x['key'] for x in objs]
        self.update_preview()
        safe_notify(f'已按 {mode} 重新排序', 'positive')

    def on_search(self, e):
        txt = str(e.value).lower().strip()

        visible_groups = set()
        visible_headers = set()

        for key, item in self.ui_groups.items():
            visible = (not txt) or (txt in item['text'])
            item['row'].set_visibility(visible)
            if visible:
                visible_groups.add(item['group_name'])
                visible_headers.add(item['header'])

        for g_name, exp in self.server_expansions.items():
            is_group_visible = g_name in visible_groups
            exp.set_visibility(is_group_visible)
            if txt and is_group_visible:
                exp.value = True

        all_headers = set(item['header'] for item in self.ui_groups.values())
        for header in all_headers:
            header.set_visibility(header in visible_headers)

    def batch_select(self, val):
        count = 0
        for key, item in self.ui_groups.items():
            if item['row'].visible:
                if val:
                    if key not in self.selected_ids:
                        self.selected_ids.append(key)
                        item['chk'].value = True
                        item['row'].classes(add='bg-blue-900/30 border-blue-500/30', remove='border-transparent')
                        count += 1
                else:
                    if key in self.selected_ids:
                        self.selected_ids.remove(key)
                        item['chk'].value = False
                        item['row'].classes(remove='bg-blue-900/30 border-blue-500/30', add='border-transparent')
                        count += 1

        if count > 0:
            self.update_preview()
            safe_notify(f"已{'添加' if val else '移除'} {count} 个节点", "positive")
        else:
            safe_notify("当前没有可操作的节点", "warning")


def open_advanced_sub_editor(sub_data=None):
    with ui.dialog() as d:
        AdvancedSubEditor(sub_data).ui(d)
        d.open()
