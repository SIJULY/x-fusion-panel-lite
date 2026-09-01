"""订阅节点解析管线。

订阅有四条出口（`/sub/{token}`、`/sub/group/{b64}`、`/get/sub/{target}/{token}`、
`/get/group/{target}/{b64}`），改造前每条都自己从 SERVERS_CACHE 里拼节点、自己解析
host，那段「取 cf_primary_domain，否则 urlparse 出 hostname」的代码在 api/subscriptions.py
里一字不差重复了 4 次。更麻烦的是过滤逻辑不一致：原始链接完全不过滤，转换链接靠
subconverter 的 include/exclude 参数过滤——同一条订阅、两个链接、两种结果。

所以把「订阅 → 最终节点列表」这件事收敛到这里一处：所有出口都调 resolve_sub_nodes，
拿到的是已经去重、过滤、改名完毕的结果。subconverter 那条路因此也不用再传
include/exclude/rename 了，只负责格式转换本身——顺带避开了 subconverter 的正则语义
和 Python re 不完全一致这个坑。
"""

import re
import time

from app.core.logging import logger
from app.core.state import (
    INDEPENDENT_NODES_CACHE,
    NODES_DATA,
    SERVERS_CACHE,
    SUBS_CACHE,
)
from app.utils.encoding import generate_node_link
from app.utils.geo import detect_country_group

# 组合订阅的递归深度上限。正常用法一两层就够，给到 5 层纯粹是兜底：
# 真正防死循环靠的是 visited 集合，深度上限只是防止有人手搓出一条极深的链。
MAX_COLLECTION_DEPTH = 5

# 判断 expiryTime 是毫秒还是秒的分界。x-ui 存的是毫秒，但不同分支/版本不一定统一，
# 而 1e11 秒 ≈ 公元 5138 年、1e11 毫秒 ≈ 1973 年，两者不可能混淆，用它当分界很安全。
_MS_THRESHOLD = 10 ** 11


def _clean_host_from_url(raw_url):
    """从服务器 url 里取出可用作节点地址的主机名。"""
    from urllib.parse import urlparse

    raw = str(raw_url or '')
    try:
        candidate = raw if '://' in raw else f'http://{raw}'
        parsed = urlparse(candidate)
        if parsed.hostname:
            return parsed.hostname
    except Exception:
        pass
    # urlparse 失败时退回手工切分，保持和改造前完全一致的行为
    return raw.split('://')[-1].split(':')[0] or raw


def _server_host(srv):
    """节点对外地址：优先 Cloudflare 主域名，其次 ssh_host（纯 IP 时），最后用服务器 url 的主机名。

    这段逻辑原先在 api/subscriptions.py 里重复了 4 次，现在只有这一处。

    关键修复：当 ssh_host 是纯 IP 且 url 里的 host 也是 IP 时，优先使用 ssh_host。
    因为用户在编辑服务器时修改的是 ssh_host，它代表了服务器的最新 IP，
    而 url 可能还保留着旧 IP（如果 save_server_config 之前的某个流程没有同步更新 url）。
    """
    cf_domain = srv.get('cf_primary_domain')
    if cf_domain and str(cf_domain).strip():
        return str(cf_domain).strip()

    # 优先使用 ssh_host（如果是纯 IP），因为它是用户最后编辑的实际服务器地址
    ssh_host = str(srv.get('ssh_host') or '').strip()
    if ssh_host:
        from app.utils.network import is_ip_literal
        if is_ip_literal(ssh_host):
            return ssh_host

    return _clean_host_from_url(srv.get('url'))


def node_key(server_url, node):
    """订阅里引用节点用的稳定标识。格式与历史数据完全一致，不能改。"""
    return f"{server_url}|{node.get('id')}"


def migrate_sub_node_keys(old_url, new_url):
    """服务器 url 变更时，同步迁移所有订阅中引用的节点 key。

    订阅的 ``nodes`` 列表存的是 ``{server_url}|{node_id}`` 格式的 key，
    服务器 url 变了之后旧 key 在 ``build_node_lookup()`` 中匹配不到，
    会被判定为「已删除服务器」的失效节点。这个函数把所有订阅中以
    ``old_url|`` 开头的 key 替换为 ``new_url|``，确保节点引用不会因
    URL 变更而断裂。

    **不做落盘**——调用方通常会在后续自行 ``save_subs()`` 或
    ``save_servers()``；在 ``save_server_config`` 的流程中，
    ``save_single_server`` 已经会触发持久化。
    """
    if not old_url or not new_url or old_url == new_url:
        return

    prefix = f"{old_url}|"
    new_prefix = f"{new_url}|"
    migrated_count = 0

    for sub in SUBS_CACHE:
        nodes = sub.get('nodes')
        if not isinstance(nodes, list):
            continue
        updated = False
        for i, key in enumerate(nodes):
            if isinstance(key, str) and key.startswith(prefix):
                nodes[i] = new_prefix + key[len(prefix):]
                updated = True
        if updated:
            migrated_count += 1

    if migrated_count:
        logger.info(f"🔄 [订阅Key迁移] {migrated_count} 条订阅的节点引用已从 {old_url} 迁移到 {new_url}")


def build_node_lookup():
    """{key: (node, host, server_conf)}，覆盖面板节点、自定义节点、独立节点。"""
    lookup = {}

    for srv in SERVERS_CACHE:
        host = _server_host(srv)
        panel_nodes = NODES_DATA.get(srv['url'], []) or []
        custom_nodes = srv.get('custom_nodes', []) or []
        for n in list(panel_nodes) + list(custom_nodes):
            if not isinstance(n, dict):
                continue
            lookup[node_key(srv['url'], n)] = (n, host, srv)

    for inode in INDEPENDENT_NODES_CACHE:
        if not isinstance(inode, dict):
            continue
        # 独立节点直接下发自己的 _raw_link，不需要 host，这里给空串保持历史行为
        lookup[f"independent|{inode['id']}"] = (inode, "", None)

    return lookup


def _find_sub_by_token(token):
    return next((s for s in SUBS_CACHE if s.get('token') == token), None)


def collect_sub_node_keys(sub, _visited=None, _depth=0):
    """展开一条订阅最终引用到的节点 key，按顺序去重。

    组合订阅（type == 'collection'）会递归展开 members。visited 集合按 token 记账，
    所以自引用（A 把自己加进 members）和环（A→B→A）都只会被展开一次就停住，不会
    死循环；深度上限只是额外兜底。
    """
    if not isinstance(sub, dict):
        return []

    if _visited is None:
        _visited = set()

    token = sub.get('token')
    if token:
        if token in _visited:
            logger.warning(f"⚠️ [订阅] 组合订阅存在循环引用，已跳过重复展开: {token}")
            return []
        _visited.add(token)

    if sub.get('type') == 'collection':
        if _depth >= MAX_COLLECTION_DEPTH:
            logger.warning(f"⚠️ [订阅] 组合订阅嵌套超过 {MAX_COLLECTION_DEPTH} 层，停止展开: {token}")
            return []

        keys = []
        seen = set()
        for member_token in sub.get('members', []) or []:
            member = _find_sub_by_token(member_token)
            if not member:
                logger.warning(f"⚠️ [订阅] 组合订阅 {sub.get('name')} 引用了不存在的成员: {member_token}")
                continue
            for key in collect_sub_node_keys(member, _visited, _depth + 1):
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        return keys

    keys = []
    seen = set()
    for key in sub.get('nodes', []) or []:
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def node_region(node, srv):
    """节点归属地区。先按节点名判断，判不出来再退回服务器。

    节点名通常自带地区信息（「香港-01」），比服务器名更准；但自定义节点的名字
    可能什么都没有，这时服务器的分组/名字才是唯一线索。
    """
    try:
        remark = str(node.get('remark') or '').strip()
        if remark:
            region = detect_country_group(remark, None)
            if region and region != '🏳️ 其他地区':
                return region
        if srv:
            return detect_country_group(srv.get('name'), srv)
    except Exception:
        pass
    return '🏳️ 其他地区'


def _compile_regex(pattern, label):
    """编译用户填的正则；填错了就当没填，并留一条日志。

    绝不能因为一个写错的正则让整条订阅 500——用户在客户端只会看到「订阅更新失败」，
    根本猜不到是自己正则写错了。
    """
    raw = str(pattern or '').strip()
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as e:
        logger.warning(f"⚠️ [订阅] {label} 正则无效，已忽略: {raw} ({e})")
        return None


_FLAG_RE = re.compile(
    # 区域指示符号对（🇦-🇿 两个连着组成国旗）以及 🏳
    r'^\s*(?:[\U0001F1E6-\U0001F1FF]{2}|\U0001F3F3)'
)


def apply_flag(name, region):
    """给节点名补地区旗帜。已经有旗帜的不动，避免叠成「🇭🇰 🇭🇰 香港-01」。"""
    if _FLAG_RE.search(name or ''):
        return name
    flag = (region or '').strip().split(' ')[0]
    if not flag or not _FLAG_RE.search(flag):
        return name
    return f"{flag} {name}".strip()


def resolve_sub_nodes(sub, lookup=None, collect_stats=False):
    """把一条订阅解析成最终节点列表。

    返回 [{key, node, host, server, region, original_name, final_name}, ...]，
    顺序即下发顺序。collect_stats=True 时返回 (结果, 各级过滤计数)，供预览用。
    """
    if lookup is None:
        lookup = build_node_lookup()

    opt = (sub or {}).get('options', {}) or {}

    include_re = _compile_regex(opt.get('include_regex'), '包含')
    exclude_re = _compile_regex(opt.get('exclude_regex'), '排除')
    rename_re = _compile_regex(opt.get('rename_pattern'), '重命名')
    rename_to = str(opt.get('rename_replacement') or '')
    regions = [str(r).strip() for r in (opt.get('regions') or []) if str(r).strip()]
    include_disabled = bool(opt.get('include_disabled', False))
    add_flag = bool(opt.get('add_flag', False))

    stats = {
        'referenced': 0,
        'missing': 0,
        'dropped_disabled': 0,
        'dropped_region': 0,
        'dropped_include': 0,
        'dropped_exclude': 0,
        'kept': 0,
    }

    resolved = []
    for key in collect_sub_node_keys(sub):
        stats['referenced'] += 1

        item = lookup.get(key)
        if not item:
            # 节点所在的服务器被删了，或 x-ui 里节点 ID 变了 —— 订阅里留下一个死 key
            stats['missing'] += 1
            continue

        node, host, srv = item

        # 与分组订阅保持一致：x-ui 里禁用的 inbound 默认不下发，
        # 否则客户端会拿到一个连不上的节点。想要例外的可以打开 include_disabled。
        if not include_disabled and not node.get('enable', True):
            stats['dropped_disabled'] += 1
            continue

        region = node_region(node, srv)
        if regions and region not in regions:
            stats['dropped_region'] += 1
            continue

        original_name = str(node.get('remark') or '未命名')

        if include_re and not include_re.search(original_name):
            stats['dropped_include'] += 1
            continue
        if exclude_re and exclude_re.search(original_name):
            stats['dropped_exclude'] += 1
            continue

        final_name = original_name
        if rename_re:
            try:
                final_name = rename_re.sub(rename_to, original_name)
            except re.error as e:
                # 替换串里的反向引用可能非法（比如 \1 但正则里没有分组）
                logger.warning(f"⚠️ [订阅] 重命名替换失败，保留原名: {e}")
                final_name = original_name
        if add_flag:
            final_name = apply_flag(final_name, region)

        final_name = final_name.strip() or original_name

        stats['kept'] += 1
        resolved.append({
            'key': key,
            'node': node,
            'host': host,
            'server': srv,
            'region': region,
            'original_name': original_name,
            'final_name': final_name,
        })

    if collect_stats:
        return resolved, stats
    return resolved


def _node_for_output(item):
    """按最终名字产出用于生成链接的节点副本。

    必须复制：node 指向 NODES_DATA / SERVERS_CACHE 里的真实对象，直接改 remark
    会把订阅的重命名结果写进面板全局状态，别处（侧边栏、仪表盘、其它订阅）都会跟着变。
    """
    node = item['node']
    if item['final_name'] == item['original_name']:
        return node
    out = dict(node)
    out['remark'] = item['final_name']
    if out.get('_raw_link'):
        # 独立节点/自定义节点直接下发 _raw_link，改名必须同步改链接末尾的 #fragment，
        # 否则客户端显示的还是旧名字。
        out['_raw_link'] = _rename_raw_link(out['_raw_link'], item['final_name'])
    return out


def _rename_raw_link(link, new_name):
    """把分享链接的备注（# 后面那段）换成新名字。"""
    from urllib.parse import quote

    raw = str(link or '')
    if not raw:
        return raw

    # vmess:// 的名字在 base64 载荷的 ps 字段里，不在 fragment 上
    if raw.startswith('vmess://'):
        try:
            import json

            from app.utils.encoding import decode_base64_safe, safe_base64

            payload = json.loads(decode_base64_safe(raw[len('vmess://'):]))
            if isinstance(payload, dict):
                payload['ps'] = new_name
                return 'vmess://' + safe_base64(json.dumps(payload, ensure_ascii=False))
        except Exception:
            return raw
        return raw

    base = raw.split('#', 1)[0]
    return f"{base}#{quote(new_name, safe='')}"


def build_sub_links(resolved):
    """生成 v2ray 系分享链接列表。"""
    links = []
    for item in resolved:
        node = _node_for_output(item)
        raw = node.get('_raw_link')
        if raw:
            links.append(raw)
            continue
        link = generate_node_link(node, item['host'])
        if link:
            links.append(link)
    return links


def build_surge_lines(resolved, lookup=None):
    """生成 Surge 配置行。

    lookup 用来解析 underlying_proxy（前置代理按名字引用另一个节点）。默认用全局索引，
    与改造前一致——前置代理允许指向订阅之外的节点。
    """
    from app.utils.encoding import generate_detail_config

    if lookup is None:
        lookup = build_node_lookup()

    lines = []
    for item in resolved:
        node = _node_for_output(item)
        prepared = _prepare_underlying_proxy(node, lookup)
        line = generate_detail_config(prepared, item['host'])
        if line and not line.startswith('//') and not line.startswith('None'):
            lines.append(line)
    return lines


def _prepare_underlying_proxy(node, lookup):
    """把 underlying_proxy 的 key 翻成 Surge 认的节点名。"""
    proxy_key = node.get('underlying_proxy')
    if not proxy_key:
        return node

    item = lookup.get(proxy_key)
    if not item:
        return node

    proxy_node = item[0]
    out = dict(node)
    out['_underlying_proxy_name'] = (
        str(proxy_node.get('remark', '')).replace(',', '_').replace('=', '_').strip()
    )
    return out


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_userinfo_header(resolved):
    """聚合出 subscription-userinfo 头的值；没有可用信息时返回 None。

    Clash / Surge / Shadowrocket / Stash 都靠这个头显示「已用流量 / 总量 / 到期」。
    两个语义坑：
      · x-ui 里 total == 0 表示**不限流量**，不是「总量为 0」。直接求和会把总量算成 0，
        客户端会显示成已经用满。所以只累加 total > 0 的节点；一个都没有就整个头不下发，
        让客户端按「无流量信息」处理。
      · expiryTime 是毫秒时间戳，且 0 表示永不过期。取所有非 0 值里**最早**的那个——
        最先到期的才是真正会影响用户的那个。
    """
    upload = download = total = 0
    limited_count = 0
    earliest_expiry = None

    for item in resolved:
        node = item['node']
        upload += _as_int(node.get('up'))
        download += _as_int(node.get('down'))

        node_total = _as_int(node.get('total'))
        if node_total > 0:
            total += node_total
            limited_count += 1

        expiry = _as_int(node.get('expiryTime'))
        if expiry > 0:
            secs = expiry // 1000 if expiry > _MS_THRESHOLD else expiry
            if earliest_expiry is None or secs < earliest_expiry:
                earliest_expiry = secs

    if limited_count == 0 and earliest_expiry is None and upload == 0 and download == 0:
        return None

    parts = [f"upload={upload}", f"download={download}"]
    if limited_count > 0:
        parts.append(f"total={total}")
    if earliest_expiry is not None:
        parts.append(f"expire={earliest_expiry}")

    return "; ".join(parts)


def group_servers(group_name):
    """分组订阅的服务器集合。与改造前的匹配规则保持一致。"""
    return [
        s for s in SERVERS_CACHE
        if s.get('group', '默认分组') == group_name or group_name in (s.get('tags') or [])
    ]


def resolve_group_nodes(group_name, lookup=None):
    """把一个分组解析成节点列表，形状与 resolve_sub_nodes 一致。

    分组订阅没有 options，行为保持原样：只过滤 enable，不改名、不筛地区。
    """
    if lookup is None:
        lookup = build_node_lookup()

    resolved = []
    for srv in group_servers(group_name):
        host = _server_host(srv)
        panel_nodes = NODES_DATA.get(srv['url'], []) or []
        custom_nodes = srv.get('custom_nodes', []) or []
        for n in list(panel_nodes) + list(custom_nodes):
            if not isinstance(n, dict) or not n.get('enable'):
                continue
            name = str(n.get('remark') or '未命名')
            resolved.append({
                'key': node_key(srv['url'], n),
                'node': n,
                'host': host,
                'server': srv,
                'region': node_region(n, srv),
                'original_name': name,
                'final_name': name,
            })
    return resolved


def record_access(token, request):
    """记一次订阅拉取，用于排查「客户端说没更新」。

    只放内存、不落库：/sub/{token} 是公网无鉴权端点，每次拉取都写一次 DB 等于留了个
    放大写入的口子——循环请求就能压库。统计信息本身也不值得为它承担这个风险，重启
    清零可以接受（UI 上已注明）。
    """
    from app.core.state import SUB_ACCESS_STATS

    if not token:
        return
    try:
        ua = ''
        ip = ''
        if request is not None:
            ua = (request.headers.get('user-agent') or '')[:200]
            ip = (
                request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                or (request.client.host if request.client else '')
            )

        entry = SUB_ACCESS_STATS.setdefault(token, {'count': 0, 'last_at': 0, 'last_ua': '', 'last_ip': ''})
        entry['count'] += 1
        entry['last_at'] = time.time()
        entry['last_ua'] = ua
        entry['last_ip'] = ip
    except Exception as e:
        # 统计失败绝不能影响订阅本身下发
        logger.warning(f"⚠️ [订阅] 记录访问统计失败: {e}")
