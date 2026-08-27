"""订阅下发端点。

四条出口全部走 app/services/sub_pipeline 的统一管线，所以「过滤 / 改名」对原始链接和
转换链接的效果完全一致——改造前原始链接根本不过滤，只有 subconverter 那条路靠
include/exclude 参数过滤，同一条订阅两个链接两种结果。
"""

import httpx
from fastapi import Request
from fastapi.responses import Response

from app.core.logging import logger
from app.core.state import ADMIN_CONFIG, SUBS_CACHE
from app.services.sub_pipeline import (
    build_node_lookup,
    build_sub_links,
    build_surge_lines,
    build_userinfo_header,
    record_access,
    resolve_group_nodes,
    resolve_sub_nodes,
)
from app.utils.encoding import decode_base64_safe, safe_base64

CONVERTER_API = "http://subconverter:25500/sub"
CONVERTER_TIMEOUT = 10.0

# 客户端 UA → subconverter target。
# 顺序有讲究：mihomo/stash 的 UA 里也常带 clash，而 sing-box 要在 shadowrocket 之前判，
# 所以按这个列表从上往下**首个命中**为准，不要改成 dict 遍历。
_UA_TARGETS = [
    ('mihomo', 'clash'),
    ('clash.meta', 'clash'),
    ('stash', 'clash'),
    ('clash', 'clash'),
    ('sing-box', 'singbox'),
    ('singbox', 'singbox'),
    ('surge', 'surge'),
    ('quantumult%20x', 'quanx'),
    ('quantumult x', 'quanx'),
    ('quantumultx', 'quanx'),
    ('loon', 'loon'),
]

# 支持的输出格式：target → 展示名。UI 的格式按钮和这里共用一份定义。
SUB_TARGETS = {
    'clash': 'Clash',
    'clashr': 'ClashR',
    'singbox': 'sing-box',
    'surge': 'Surge',
    'quanx': 'Quantumult X',
    'loon': 'Loon',
    # Shadowrocket 吃的就是 base64，subconverter 里没有单独的 shadowrocket target，
    # 所以标签里点明白，免得用户在列表里找不到自己的客户端。
    'v2ray': 'V2Ray / Shadowrocket (Base64)',
    'ss': 'Shadowsocks',
}


def detect_target_from_ua(user_agent):
    """按客户端 UA 猜输出格式；猜不出来返回 None（调用方回落 base64）。"""
    ua = (user_agent or '').lower()
    if not ua:
        return None
    for needle, target in _UA_TARGETS:
        if needle in ua:
            return target
    return None


def _base_url(request):
    """subconverter 回来抓原始订阅时用的地址。

    必须是 subconverter 容器能访问到的地址：优先用管理员配的主控地址，否则用请求头里的
    Host 拼——两个容器在同一 docker 网络里，Host 通常就是可达的。
    """
    custom_base = str(ADMIN_CONFIG.get('manager_base_url', '') or '').strip().rstrip('/')
    if custom_base:
        return custom_base
    if request is None:
        return 'http://localhost'
    host = request.headers.get('host', 'localhost')
    return f"{request.url.scheme}://{host}"


def _plain(content, userinfo=None, filename=None):
    headers = {}
    if userinfo:
        # 头名必须是全小写连字符，Clash / Surge / Shadowrocket / Stash 都认这个
        headers['subscription-userinfo'] = userinfo
    if filename:
        headers['content-disposition'] = f'attachment; filename*=UTF-8\'\'{filename}'
    return Response(content, media_type="text/plain; charset=utf-8", headers=headers or None)


def _converter_params(target, internal_api, opt):
    """subconverter 参数。

    注意**不再传** include / exclude / rename：过滤和改名已经由管线在原始订阅里做完了。
    再传一遍等于做两次，而且 subconverter 的正则语义和 Python re 不完全一致，两边结果
    可能不同。这里只留纯格式相关的开关。
    """
    return {
        "target": target,
        "url": internal_api,
        "insert": "false",
        "list": "true",
        "ver": "4",
        "emoji": str(bool(opt.get('emoji', False))).lower(),
        "udp": str(bool(opt.get('udp', True))).lower(),
        "tfo": str(bool(opt.get('tfo', False))).lower(),
        "scv": str(bool(opt.get('skip_cert', True))).lower(),
        "fdn": "false",
        "sort": "false",
    }


async def _call_converter(params):
    """调 subconverter。返回 (content, error)，只有一个非空。"""
    try:
        async with httpx.AsyncClient(timeout=CONVERTER_TIMEOUT) as client:
            response = await client.get(CONVERTER_API, params=params)
        if response.status_code == 200:
            return response.content, None
        return None, f"SubConverter Error (Code: {response.status_code})"
    except Exception as e:
        return None, f"SubConverter Error: {e}"


async def sub_handler(token: str, request: Request = None):
    """原始订阅出口。按客户端 UA 自动返回对应格式，认不出来就给 base64。"""
    sub = next((s for s in SUBS_CACHE if s.get('token') == token), None)
    if not sub:
        return Response("Invalid Token", 404)

    record_access(token, request)

    resolved = resolve_sub_nodes(sub)
    userinfo = build_userinfo_header(resolved)
    b64 = safe_base64("\n".join(build_sub_links(resolved)))

    # 显式 ?target= 优先于 UA 判断，方便排查「我的客户端到底拿到了什么」
    explicit = None
    if request is not None:
        explicit = (request.query_params.get('target') or '').strip().lower() or None

    if explicit == 'v2ray':
        return _plain(b64, userinfo)

    target = explicit or detect_target_from_ua(
        request.headers.get('user-agent') if request is not None else None
    )
    if not target or target == 'v2ray':
        return _plain(b64, userinfo)

    if target == 'surge':
        lines = build_surge_lines(resolved)
        if lines:
            return _plain("\n".join(lines), userinfo)
        return _plain(b64, userinfo)

    internal_api = f"{_base_url(request)}/sub/{token}?target=v2ray"
    content, error = await _call_converter(
        _converter_params(target, internal_api, sub.get('options', {}) or {})
    )
    if error:
        # UA 自动路径**绝不**返回错误页：客户端只会显示「订阅更新失败」，用户完全
        # 摸不着头脑。回落 base64 至少维持改造前的行为。
        logger.warning(f"⚠️ [订阅] UA 自适应转换失败，已回落 base64 ({target}): {error}")
        return _plain(b64, userinfo)

    return Response(content, media_type="text/plain; charset=utf-8",
                    headers={'subscription-userinfo': userinfo} if userinfo else None)


async def group_sub_handler(group_b64: str):
    """分组订阅（原始链接）。行为与改造前一致：只过滤 enable。"""
    group_name = decode_base64_safe(group_b64)
    if not group_name:
        return Response("Invalid Group Name", 400)

    resolved = resolve_group_nodes(group_name)
    logger.info(f"正在生成分组订阅: [{group_name}]，匹配到 {len(resolved)} 个节点")

    links = build_sub_links(resolved)
    if not links:
        return _plain(f"// Group [{group_name}] is empty or not found")

    return _plain(safe_base64("\n".join(links)), build_userinfo_header(resolved))


async def short_group_handler(target: str, group_b64: str, request: Request):
    try:
        group_name = decode_base64_safe(group_b64)
        if not group_name:
            return Response("Invalid Group Name", 400)

        resolved = resolve_group_nodes(group_name)
        userinfo = build_userinfo_header(resolved)

        if target == 'surge':
            lines = build_surge_lines(resolved)
            if not lines:
                return _plain(f"// Group [{group_name}] is empty")
            return _plain("\n".join(lines), userinfo)

        internal_api = f"{_base_url(request)}/sub/group/{group_b64}"
        content, error = await _call_converter(_converter_params(target, internal_api, {}))
        if error:
            # 用户显式点了某个格式，就该看到真实错误——而不是悄悄给一份别的格式
            return Response(error, status_code=502)

        return Response(content, media_type="text/plain; charset=utf-8",
                        headers={'subscription-userinfo': userinfo} if userinfo else None)

    except Exception as e:
        logger.error(f"❌ [订阅] 分组转换订阅出错 [{target}]: {e}")
        return Response(f"Error: {e}", status_code=500)


async def short_sub_handler(target: str, token: str, request: Request):
    try:
        sub_obj = next((s for s in SUBS_CACHE if s.get('token') == token), None)
        if not sub_obj:
            return Response("Subscription Not Found", 404)

        record_access(token, request)

        lookup = build_node_lookup()
        resolved = resolve_sub_nodes(sub_obj, lookup)
        userinfo = build_userinfo_header(resolved)

        if target == 'v2ray':
            return _plain(safe_base64("\n".join(build_sub_links(resolved))), userinfo)

        if target == 'surge':
            return _plain("\n".join(build_surge_lines(resolved, lookup)), userinfo)

        # 让 subconverter 明确抓 base64 原文，避免它自己的 UA 触发上面的自适应逻辑
        internal_api = f"{_base_url(request)}/sub/{token}?target=v2ray"
        content, error = await _call_converter(
            _converter_params(target, internal_api, sub_obj.get('options', {}) or {})
        )
        if error:
            return Response(error, status_code=502)

        return Response(content, media_type="text/plain; charset=utf-8",
                        headers={'subscription-userinfo': userinfo} if userinfo else None)

    except Exception as e:
        logger.error(f"❌ [订阅] 转换订阅出错 [{target}]: {e}")
        return Response(f"Error: {e}", status_code=500)
