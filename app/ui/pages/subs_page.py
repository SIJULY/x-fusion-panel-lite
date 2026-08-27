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

    with ui.dialog() as d, ui.card().classes('w-[380px] p-0 gap-0 overflow-hidden rounded-sm bg-[#070b14] border border-[#1e3a5f]/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-[380px] p-0 gap-0 overflow-hidden rounded-sm bg-white border border-slate-300/90 shadow-[0_10px_28px_rgba(148,163,184,0.18)]'):
        with ui.row().classes('w-full justify-between items-center p-4 border-b border-[#1e3a5f]/60 bg-gradient-to-r from-[#0a1526] to-[#050a14]' if is_dark else 'w-full justify-between items-center p-4 border-b border-slate-300/90 bg-gradient-to-r from-[#f8fbff] to-[#eaf2ff]'):
            ui.label(f'扫码导入 · {title}').classes('text-base font-black text-slate-100 tracking-wide truncate' if is_dark else 'text-base font-black text-slate-800 tracking-wide truncate')
            ui.button(icon='close', on_click=d.close).props('flat round dense color=grey')

        with ui.column().classes('w-full p-4 gap-3 items-center bg-[#030712]' if is_dark else 'w-full p-4 gap-3 items-center bg-[#f8fbff]'):
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
                        ui.image(data).style('width: 220px; height: 220px').classes('rounded-sm bg-white p-2 border border-slate-300')
                    else:
                        ui.label('二维码生成失败（qrcode 库不可用），可直接复制下面的链接').classes('text-xs text-amber-400 text-center')
                    with ui.row().classes('w-full items-center gap-2 p-2 rounded-sm border cursor-pointer').style('background: var(--xf-code-bg); border-color: var(--xf-card-border);').on('click', lambda u=url: safe_copy_to_clipboard(u)):
                        ui.label(url).classes('text-[10px] font-mono flex-grow break-all').style('color: var(--xf-text-strong);')
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
    parts = [f'📡 已拉取 {count} 次', f'最后 {when}']
    if ua:
        parts.append(ua[:28])
    return ' · '.join(parts)


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
    content_container.classes(remove='justify-center items-center overflow-hidden p-6', add='h-full overflow-y-auto p-4 pl-6 justify-start')
    content_container.style('background-color: var(--xf-bg-main);')

    # 用管线的索引当「有效 key」的唯一标准，页面显示的有效数就等于订阅真正能解析出的
    # 节点数。原来这里只扫 SERVERS_CACHE，独立节点不在其中，于是引用了独立节点的订阅
    # 会被算成「失效」——数字是错的，而现在多了一键清理，照着错数字清会误删。
    lookup = build_node_lookup()
    all_active_keys = set(lookup.keys())

    with content_container:
        page_header_cls = 'w-full mb-5 justify-between items-center border-b pb-3'
        page_header_style = 'border-color: var(--xf-card-border);'
        page_icon_cls = 'w-10 h-10 rounded-sm flex items-center justify-center border relative overflow-hidden'
        page_icon_style = 'background: var(--xf-code-bg); border-color: var(--xf-card-border); color: var(--xf-accent); box-shadow: 0 4px 12px rgba(15,23,42,0.12);'
        page_title_cls = 'text-2xl font-black tracking-wide'
        page_title_style = 'color: var(--xf-text-strong);'
        card_cls = 'w-full p-4 mb-3 transition border border-l-4 rounded-sm'
        card_style = 'background: var(--xf-panel-bg); border-color: var(--xf-card-border); box-shadow: 0 8px 24px rgba(15,23,42,0.10);'

        with ui.row().classes(page_header_cls).style(page_header_style):
            with ui.row().classes('items-center gap-3'):
                with ui.element('div').classes(page_icon_cls).style(page_icon_style):
                    ui.element('div').classes('absolute inset-0').style('background: var(--xf-accent-soft);')
                    ui.icon('rss_feed').classes('text-[18px] drop-shadow-[0_0_5px_currentColor]')
                ui.label('订阅管理').classes(page_title_cls).style(page_title_style)
            ui.button('新建订阅', icon='add', on_click=lambda: open_advanced_sub_editor(None)).props('flat').classes('bg-emerald-950/45 text-emerald-300 border border-emerald-500/45 hover:bg-emerald-900/55 font-black rounded-sm px-4' if is_dark else 'bg-emerald-100 text-emerald-700 border border-emerald-300 hover:bg-emerald-200 font-black rounded-sm px-4')

        if not SUBS_CACHE:
            with ui.column().classes('w-full h-64 justify-center items-center border border-dashed rounded-sm').style('background: var(--xf-panel-bg); border-color: var(--xf-card-border); color: var(--xf-text-muted);'):
                ui.icon('rss_feed', size='4rem').style('color: var(--xf-accent); opacity: 0.8;')
                ui.label('暂无订阅').classes('text-sm font-bold').style('color: var(--xf-text-muted);')

        for idx, sub in enumerate(SUBS_CACHE):
            is_collection = sub.get('type') == 'collection'
            token = sub.get('token', '')

            saved_node_ids = set(sub.get('nodes', []) or [])
            valid_count = len(saved_node_ids.intersection(all_active_keys))
            total_count = len(saved_node_ids)
            dead_keys = [k for k in (sub.get('nodes', []) or []) if k not in all_active_keys]

            try:
                delivered = len(resolve_sub_nodes(sub, lookup=lookup))
            except Exception:
                delivered = None

            with ui.card().classes(card_cls).style(card_style):
                with ui.row().classes('justify-between w-full items-start'):
                    with ui.column().classes('gap-1'):
                        with ui.row().classes('items-center gap-2'):
                            ui.label(sub.get('name', '未命名订阅')).classes('font-black text-lg tracking-wide').style('color: var(--xf-text-strong);')
                            if is_collection:
                                ui.badge('组合', color='purple').props('outline size=xs').classes('text-purple-300 border-purple-500/45 rounded-sm' if is_dark else 'text-purple-700 border-purple-300 rounded-sm').tooltip('节点来自多个成员订阅的合并')
                            else:
                                ui.badge('普通', color='cyan').props('outline size=xs').classes('text-cyan-300 border-cyan-500/45 rounded-sm' if is_dark else 'text-sky-700 border-sky-300 rounded-sm')

                        if is_collection:
                            members = sub.get('members', []) or []
                            names = []
                            for mt in members:
                                m = next((s for s in SUBS_CACHE if s.get('token') == mt), None)
                                names.append(m.get('name', '未命名') if m else f'⚠️ 已失效({mt[:6]})')
                            ui.label(f"🧩 合并 {len(members)} 个订阅 → 实际下发 {delivered if delivered is not None else '?'} 个节点") \
                                .classes('text-xs font-bold text-purple-400 font-mono')
                            if names:
                                ui.label('成员: ' + ' / '.join(names)).classes('text-[11px] font-mono').style('color: var(--xf-text-muted);')
                        else:
                            color_cls = 'text-green-400' if valid_count > 0 else 'text-slate-500'
                            ui.label(f"⚡ 包含节点: {valid_count} (有效) / {total_count} (总计)").classes(f'text-xs font-bold {color_cls} font-mono')
                            # 有效数 ≠ 实际下发数，说明筛选规则把节点刷掉了。不点开预览也能看见，
                            # 免得「明明选了 20 个客户端只有 3 个」还得去猜。
                            if delivered is not None and delivered != valid_count:
                                ui.label(f"🔎 筛选后实际下发: {delivered} 个").classes('text-[11px] font-bold text-amber-400 font-mono')

                        acc = access_text(token)
                        if acc:
                            ui.label(acc).classes('text-[11px] font-mono').style('color: var(--xf-text-muted);').tooltip('访问统计只放内存，面板重启后清零')

                    with ui.row().classes('gap-2 items-center'):
                        if dead_keys:
                            async def do_clean(s=sub, dead=list(dead_keys)):
                                with ui.dialog() as cd, ui.card().classes('w-[380px] p-0 gap-0 overflow-hidden rounded-sm bg-[#070b14] border border-amber-800/55' if is_dark else 'w-[380px] p-0 gap-0 overflow-hidden rounded-sm bg-white border border-amber-300'):
                                    with ui.column().classes('w-full p-5 gap-2 bg-gradient-to-r from-[#1a1405] to-[#0b0911] border-b border-amber-900/60' if is_dark else 'w-full p-5 gap-2 bg-gradient-to-r from-amber-50 to-orange-50 border-b border-amber-200'):
                                        ui.label(f'清理 {len(dead)} 个失效节点？').classes('font-black text-amber-300 text-lg tracking-wide' if is_dark else 'font-black text-amber-700 text-lg tracking-wide')
                                        ui.label('这些节点所在的服务器已被删除，或在 x-ui 里的 ID 变了，订阅里只剩一个连不上的死引用。').classes('text-xs text-slate-400')
                                    with ui.row().classes('justify-end w-full p-4 bg-[#030712] gap-2' if is_dark else 'justify-end w-full p-4 bg-white gap-2'):
                                        ui.button('取消', on_click=cd.close).props('outline color=grey').classes('text-slate-300 border-slate-600 text-xs font-bold rounded-sm' if is_dark else 'text-slate-600 border-slate-300 text-xs font-bold rounded-sm')

                                        async def confirm_clean():
                                            removed = 0
                                            kept = []
                                            for k in (s.get('nodes', []) or []):
                                                if k in all_active_keys:
                                                    kept.append(k)
                                                else:
                                                    removed += 1
                                            s['nodes'] = kept
                                            await save_subs()
                                            cd.close()
                                            await load_subs_view()
                                            safe_notify(f'已清理 {removed} 个失效节点', 'positive')

                                        ui.button('确认清理', on_click=confirm_clean).props('flat').classes('bg-amber-950/45 text-amber-300 border border-amber-500/45 hover:bg-amber-900/55 font-black rounded-sm px-4' if is_dark else 'bg-amber-100 text-amber-700 border border-amber-300 hover:bg-amber-200 font-black rounded-sm px-4')
                                cd.open()

                            ui.button(f'清理失效 ({len(dead_keys)})', icon='cleaning_services', on_click=do_clean) \
                                .props('flat dense size=sm') \
                                .classes('rounded-sm px-3 font-black border bg-amber-950/35 text-amber-300 border-amber-500/45 hover:bg-amber-900/50' if is_dark else 'rounded-sm px-3 font-black border bg-amber-100 text-amber-700 border-amber-300 hover:bg-amber-200') \
                                .tooltip('剔除指向已删服务器 / 已变 ID 的节点')

                        ui.button('管理订阅', icon='tune', on_click=lambda _, s=sub: open_advanced_sub_editor(s)) \
                            .props('flat dense size=sm') \
                            .classes('rounded-sm px-3 font-black border') \
                            .style('background: var(--xf-soft-bg); color: var(--xf-accent); border-color: var(--xf-card-border);') \
                            .tooltip('重命名 / 排序 / 筛选节点')

                        async def dl(i=idx):
                            with ui.dialog() as d, ui.card().classes('w-[360px] p-0 gap-0 overflow-hidden rounded-sm bg-[#070b14] border border-rose-800/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-[360px] p-0 gap-0 overflow-hidden rounded-sm bg-white border border-rose-300 shadow-[0_10px_28px_rgba(148,163,184,0.18)]'):
                                with ui.column().classes('w-full p-5 gap-3 bg-gradient-to-r from-[#19070d] to-[#0b0911] border-b border-rose-900/60' if is_dark else 'w-full p-5 gap-3 bg-gradient-to-r from-rose-50 to-orange-50 border-b border-rose-200'):
                                    ui.label('确定删除此订阅？').classes('font-black text-rose-300 text-lg tracking-wide' if is_dark else 'font-black text-rose-700 text-lg tracking-wide')
                                    # 被别的组合订阅引用时先提醒，否则那条组合订阅会静默少掉一批节点
                                    tok = SUBS_CACHE[i].get('token')
                                    refs = [s.get('name', '未命名') for s in SUBS_CACHE
                                            if s.get('type') == 'collection' and tok in (s.get('members', []) or [])]
                                    if refs:
                                        ui.label('⚠️ 以下组合订阅正在引用它：' + '、'.join(refs)).classes('text-xs font-bold text-amber-400')
                                with ui.row().classes('justify-end w-full mt-4 p-4 bg-[#030712] gap-2' if is_dark else 'justify-end w-full mt-4 p-4 bg-white gap-2'):
                                    ui.button('取消', on_click=d.close).props('outline color=grey').classes('text-slate-300 border-slate-600 hover:bg-slate-800/40 text-xs font-bold tracking-wide rounded-sm' if is_dark else 'text-slate-600 border-slate-300 hover:bg-slate-100 text-xs font-bold tracking-wide rounded-sm')

                                    async def confirm():
                                        del SUBS_CACHE[i]
                                        await save_subs()
                                        await load_subs_view()
                                        d.close()
                                        safe_notify('已删除', 'positive')

                                    ui.button('删除', on_click=confirm).props('flat').classes('bg-rose-950/45 text-rose-300 border border-rose-500/45 hover:bg-rose-900/55 font-black rounded-sm px-4' if is_dark else 'bg-rose-100 text-rose-700 border border-rose-300 hover:bg-rose-200 font-black rounded-sm px-4')
                            d.open()

                        ui.button(icon='delete', on_click=dl).props('flat dense size=sm').classes('text-rose-400 hover:bg-rose-950/30 hover:text-rose-300')

                ui.separator().classes('my-3 opacity-80').style('background: var(--xf-card-border);')

                path = f"/sub/{token}"
                raw_url = f"{origin}{path}"

                with ui.row().classes('w-full items-center gap-2 p-2.5 rounded-sm justify-between border').style('background: var(--xf-code-bg); border-color: var(--xf-card-border);'):
                    with ui.row().classes('items-center gap-3 flex-grow overflow-hidden'):
                        ui.icon('link').classes('text-sm').style('color: var(--xf-accent);')
                        ui.label(raw_url).classes('text-xs font-mono font-bold truncate select-all').style('color: var(--xf-text-strong);')

                    with ui.row().classes('gap-1 items-center flex-shrink-0'):
                        def btn_copy(icon, color, text, func):
                            ui.button(icon=icon, on_click=func).props(f'flat dense round size=xs text-color={color}').tooltip(text).style('color: var(--xf-text-muted);')

                        btn_copy('content_copy', 'grey-4', '复制原始链接（按客户端 UA 自动返回对应格式）', lambda u=raw_url: safe_copy_to_clipboard(u))

                        surge_short = f"{origin}/get/sub/surge/{token}"
                        btn_copy('bolt', 'orange', '复制 Surge 订阅', lambda u=surge_short: safe_copy_to_clipboard(u))

                        clash_short = f"{origin}/get/sub/clash/{token}"
                        btn_copy('cloud_queue', 'green', '复制 Clash 订阅', lambda u=clash_short: safe_copy_to_clipboard(u))

                        pairs = sub_url_pairs(origin, token)

                        btn_copy('qr_code_2', 'cyan',
                                 '扫码导入（可切换格式）',
                                 lambda n=sub.get('name', '订阅'), p=pairs: open_qr_dialog(n, p))

                        with ui.button(icon='more_horiz').props('flat dense round size=xs text-color=grey-4').style('color: var(--xf-text-muted);').tooltip('全部输出格式'):
                            with ui.menu().props('auto-close').classes('bg-[#070b14] border border-[#1e3a5f]/55' if is_dark else 'bg-white border border-slate-300'):
                                for t in ordered_targets():
                                    url = f"{origin}/get/sub/{t}/{token}"
                                    ui.menu_item(f"复制 {SUB_TARGETS[t]} 链接",
                                                 on_click=lambda u=url: safe_copy_to_clipboard(u)) \
                                        .classes('text-xs')
                                ui.separator()
                                ui.menu_item('复制原始链接（自适应）',
                                             on_click=lambda u=raw_url: safe_copy_to_clipboard(u)).classes('text-xs')

        ui.separator().classes('my-6 opacity-80').style('background: var(--xf-card-border);')

        with ui.row().classes(page_header_cls).style(page_header_style):
            with ui.row().classes('items-center gap-3'):
                with ui.element('div').classes(page_icon_cls).style(page_icon_style):
                    ui.element('div').classes('absolute inset-0').style('background: var(--xf-accent-soft);')
                    ui.icon('hub').classes('text-[18px] drop-shadow-[0_0_5px_currentColor]')
                ui.label('独立节点管理').classes(page_title_cls).style(page_title_style)

            with ui.row().classes('gap-2'):
                def open_batch_import():
                    from app.ui.dialogs.sub_dialogs import open_batch_import_dialog
                    open_batch_import_dialog()

                ui.button('批量导入', icon='playlist_add', on_click=open_batch_import) \
                    .props('flat') \
                    .classes('bg-cyan-950/45 text-cyan-300 border border-cyan-500/45 hover:bg-cyan-900/55 font-black rounded-sm px-4' if is_dark else 'bg-sky-100 text-sky-700 border border-sky-300 hover:bg-sky-200 font-black rounded-sm px-4') \
                    .tooltip('一次粘贴多条分享链接')

                def open_add_independent_node():
                    from app.ui.dialogs.sub_dialogs import open_independent_node_editor
                    open_independent_node_editor(None)

                ui.button('添加独立节点', icon='add', on_click=open_add_independent_node).props('flat').classes('bg-blue-950/45 text-blue-300 border border-blue-500/45 hover:bg-blue-900/55 font-black rounded-sm px-4' if is_dark else 'bg-blue-100 text-blue-700 border border-blue-300 hover:bg-blue-200 font-black rounded-sm px-4')

        if not INDEPENDENT_NODES_CACHE:
            with ui.column().classes('w-full h-64 justify-center items-center border border-dashed rounded-sm').style('background: var(--xf-panel-bg); border-color: var(--xf-card-border); color: var(--xf-text-muted);'):
                ui.icon('hub', size='4rem').style('color: var(--xf-accent); opacity: 0.8;')
                ui.label('暂无独立节点').classes('text-sm font-bold').style('color: var(--xf-text-muted);')

        for idx, inode in enumerate(INDEPENDENT_NODES_CACHE):
            with ui.card().classes(card_cls).style(card_style):
                with ui.row().classes('justify-between w-full items-start'):
                    with ui.column().classes('gap-1'):
                        with ui.row().classes('items-center gap-2'):
                            ui.label(inode.get('remark', '未命名节点')).classes('font-black text-lg tracking-wide').style('color: var(--xf-text-strong);')
                            ui.badge('独立', color='blue').props('outline size=xs').classes('text-blue-300 border-blue-500/45 rounded-sm' if is_dark else 'text-blue-700 border-blue-300 rounded-sm')

                        link = inode.get('_raw_link', '')
                        protocol = "unknown"
                        if link:
                            protocol = link.split('://')[0]
                        ui.label(f"⚡ 协议: {protocol} | ID: {inode.get('id', 'N/A')}").classes('text-xs font-bold text-slate-500 font-mono')

                        # 被哪些订阅引用了。删之前能看见影响面，不用一条条订阅点开找。
                        ikey = f"independent|{inode.get('id')}"
                        used_by = [s.get('name', '未命名') for s in SUBS_CACHE if ikey in (s.get('nodes', []) or [])]
                        if used_by:
                            ui.label('🔗 被订阅引用: ' + '、'.join(used_by)).classes('text-[11px] font-mono').style('color: var(--xf-text-muted);')

                    with ui.row().classes('gap-2'):
                        def edit_inode(node=inode):
                            from app.ui.dialogs.sub_dialogs import open_independent_node_editor
                            open_independent_node_editor(node)

                        ui.button('编辑', icon='edit', on_click=edit_inode) \
                            .props('flat dense size=sm') \
                            .classes('rounded-sm px-3 font-black border') \
                            .style('background: var(--xf-soft-bg); color: var(--xf-accent); border-color: var(--xf-card-border);')

                        async def dl_inode(i=idx):
                            with ui.dialog() as d, ui.card().classes('w-[360px] p-0 gap-0 overflow-hidden rounded-sm bg-[#070b14] border border-rose-800/55 shadow-[0_18px_48px_rgba(0,0,0,0.78)]' if is_dark else 'w-[360px] p-0 gap-0 overflow-hidden rounded-sm bg-white border border-rose-300 shadow-[0_10px_28px_rgba(148,163,184,0.18)]'):
                                with ui.column().classes('w-full p-5 gap-3 bg-gradient-to-r from-[#19070d] to-[#0b0911] border-b border-rose-900/60' if is_dark else 'w-full p-5 gap-3 bg-gradient-to-r from-rose-50 to-orange-50 border-b border-rose-200'):
                                    ui.label('确定删除此独立节点？').classes('font-black text-rose-300 text-lg tracking-wide' if is_dark else 'font-black text-rose-700 text-lg tracking-wide')
                                    ik = f"independent|{INDEPENDENT_NODES_CACHE[i].get('id')}"
                                    refs = [s.get('name', '未命名') for s in SUBS_CACHE if ik in (s.get('nodes', []) or [])]
                                    if refs:
                                        ui.label('⚠️ 以下订阅正在使用它：' + '、'.join(refs)).classes('text-xs font-bold text-amber-400')
                                with ui.row().classes('justify-end w-full mt-4 p-4 bg-[#030712] gap-2' if is_dark else 'justify-end w-full mt-4 p-4 bg-white gap-2'):
                                    ui.button('取消', on_click=d.close).props('outline color=grey').classes('text-slate-300 border-slate-600 hover:bg-slate-800/40 text-xs font-bold tracking-wide rounded-sm' if is_dark else 'text-slate-600 border-slate-300 hover:bg-slate-100 text-xs font-bold tracking-wide rounded-sm')

                                    async def confirm():
                                        del INDEPENDENT_NODES_CACHE[i]
                                        await save_independent_nodes()
                                        await load_subs_view()
                                        d.close()
                                        safe_notify('已删除独立节点', 'positive')

                                    ui.button('删除', on_click=confirm).props('flat').classes('bg-rose-950/45 text-rose-300 border border-rose-500/45 hover:bg-rose-900/55 font-black rounded-sm px-4' if is_dark else 'bg-rose-100 text-rose-700 border border-rose-300 hover:bg-rose-200 font-black rounded-sm px-4')
                            d.open()

                        ui.button(icon='delete', on_click=dl_inode).props('flat dense size=sm').classes('text-rose-400 hover:bg-rose-950/30 hover:text-rose-300')

                ui.separator().classes('my-3 opacity-80').style('background: var(--xf-card-border);')

                with ui.row().classes('w-full items-center gap-2 p-2.5 rounded-sm justify-between border').style('background: var(--xf-code-bg); border-color: var(--xf-card-border);'):
                    with ui.row().classes('items-center gap-3 flex-grow overflow-hidden'):
                        ui.icon('link').classes('text-sm').style('color: var(--xf-accent);')
                        ui.label(inode.get('_raw_link', '')).classes('text-xs font-mono font-bold truncate select-all').style('color: var(--xf-text-strong);')

                    with ui.row().classes('gap-1'):
                        def btn_copy(icon, color, text, func):
                            ui.button(icon=icon, on_click=func).props(f'flat dense round size=xs text-color={color}').tooltip(text).style('color: var(--xf-text-muted);')

                        btn_copy('content_copy', 'grey-4', '复制节点链接', lambda u=inode.get('_raw_link', ''): safe_copy_to_clipboard(u))
