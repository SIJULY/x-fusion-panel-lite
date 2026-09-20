import base64
import json
import re
from urllib.parse import quote

from app.core.logging import logger


def _load_json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except:
            return {}
    return {}


def _get_settings(node):
    return _load_json_object(node.get('settings'))


def _get_stream_settings(node):
    return _load_json_object(node.get('streamSettings') or node.get('stream_settings'))


def _clean_server_host(server_host):
    clean_host = str(server_host or '').replace('http://', '').replace('https://', '')
    if ':' in clean_host and not clean_host.startswith('['):
        clean_host = clean_host.split(':')[0]
    return clean_host


def _url_quote(value):
    return quote(str(value or ''), safe='')


def _stringify_vmess_port(port):
    try:
        return str(int(port))
    except:
        return str(port)


def _append_underlying_proxy(line, node):
    proxy_name = str(node.get('_underlying_proxy_name') or node.get('underlying_proxy') or '').strip()
    if proxy_name:
        safe_proxy_name = proxy_name.replace(',', '_').replace('=', '_').strip()
        return f"{line}, underlying-proxy={safe_proxy_name}"
    return line


def _build_vmess_link(node, address):
    settings = _get_settings(node)
    stream = _get_stream_settings(node)
    clients = settings.get('clients') or [{}]
    client = clients[0] if clients else {}
    remark = node.get('remark', 'Unnamed')
    port = node.get('port', '')
    net = stream.get('network', 'tcp')
    security = stream.get('security', 'none')

    host = ''
    path = ''
    if net == 'ws':
        ws_settings = stream.get('wsSettings', {})
        host = ws_settings.get('headers', {}).get('Host', '')
        path = ws_settings.get('path', '/')
    elif net == 'grpc':
        path = stream.get('grpcSettings', {}).get('serviceName', '')
    elif net == 'httpupgrade':
        httpupgrade_settings = stream.get('httpupgradeSettings', {})
        host = httpupgrade_settings.get('host', '')
        path = httpupgrade_settings.get('path', '/')
    elif net == 'xhttp':
        xhttp_settings = stream.get('xhttpSettings', {})
        host = xhttp_settings.get('host', '')
        path = xhttp_settings.get('path', '')

    tls_value = security if security in ['tls', 'reality'] else 'none'

    vmess_payload = {
        'v': '2',
        'ps': remark,
        'add': address,
        'port': _stringify_vmess_port(port),
        'id': client.get('id', ''),
        'aid': str(client.get('alterId', 0)),
        'scy': client.get('security', 'auto') or 'auto',
        'net': net,
        'type': 'none',
        'host': host,
        'path': path,
        'tls': tls_value,
    }

    server_name = stream.get('tlsSettings', {}).get('serverName', '')
    if server_name:
        vmess_payload['sni'] = server_name

    return 'vmess://' + safe_base64(json.dumps(vmess_payload, ensure_ascii=False))


def _build_vless_link(node, address):
    settings = _get_settings(node)
    stream = _get_stream_settings(node)
    clients = settings.get('clients') or [{}]
    client = clients[0] if clients else {}
    remark = node.get('remark', 'Unnamed')
    port = node.get('port', '')
    net = stream.get('network', 'tcp')
    security = stream.get('security', 'none')

    params = [
        f'type={_url_quote(net)}',
        f'security={_url_quote(security)}',
    ]

    if net == 'ws':
        ws_settings = stream.get('wsSettings', {})
        path = ws_settings.get('path', '/')
        host = ws_settings.get('headers', {}).get('Host', '')
        if path:
            params.append(f'path={_url_quote(path)}')
        if host:
            params.append(f'host={_url_quote(host)}')
    elif net == 'grpc':
        service_name = stream.get('grpcSettings', {}).get('serviceName', '')
        if service_name:
            params.append(f'serviceName={_url_quote(service_name)}')
    elif net == 'xhttp':
        xhttp_settings = stream.get('xhttpSettings', {})
        path = xhttp_settings.get('path', '')
        host = xhttp_settings.get('host', '')
        mode = xhttp_settings.get('mode', '')
        if path:
            params.append(f'path={_url_quote(path)}')
        if host:
            params.append(f'host={_url_quote(host)}')
        if mode:
            params.append(f'mode={_url_quote(mode)}')

    tls_settings = stream.get('tlsSettings', {})
    server_name = tls_settings.get('serverName', '')
    if server_name:
        params.append(f'sni={_url_quote(server_name)}')

    reality_settings = stream.get('realitySettings', {})
    if security == 'reality':
        if reality_settings.get('serverName'):
            params.append(f'sni={_url_quote(reality_settings.get("serverName"))}')
        if reality_settings.get('publicKey'):
            params.append(f'pbk={_url_quote(reality_settings.get("publicKey"))}')
        short_id = reality_settings.get('shortId')
        if isinstance(short_id, list):
            short_id = short_id[0] if short_id else ''
        if short_id:
            params.append(f'sid={_url_quote(short_id)}')
        flow = client.get('flow', '')
        if flow:
            params.append(f'flow={_url_quote(flow)}')

    query = '&'.join(params)
    return f"vless://{client.get('id', '')}@{address}:{port}?{query}#{_url_quote(remark)}"


def _build_trojan_link(node, address):
    settings = _get_settings(node)
    stream = _get_stream_settings(node)
    clients = settings.get('clients') or [{}]
    client = clients[0] if clients else {}
    remark = node.get('remark', 'Unnamed')
    port = node.get('port', '')
    net = stream.get('network', 'tcp')
    security = stream.get('security', 'none')
    params = [
        f'type={_url_quote(net)}',
        f'security={_url_quote(security)}',
    ]
    server_name = stream.get('tlsSettings', {}).get('serverName', '')
    if server_name:
        params.append(f'sni={_url_quote(server_name)}')
    return f"trojan://{client.get('password', '')}@{address}:{port}?{'&'.join(params)}#{_url_quote(remark)}"


def _build_shadowsocks_link(node, address):
    settings = _get_settings(node)
    remark = node.get('remark', 'Unnamed')
    port = node.get('port', '')
    cred = f"{settings.get('method', '')}:{settings.get('password', '')}"
    return f"ss://{safe_base64(cred)}@{address}:{port}#{_url_quote(remark)}"


def parse_vless_link_to_node(link, remark_override=None):
    """将 vless:// 链接解析为面板节点格式的字典"""
    try:
        if not link.startswith("vless://"):
            return None

        import urllib.parse

        main_part = link.replace("vless://", "")

        remark = "XHTTP-Reality"
        if "#" in main_part:
            main_part, remark = main_part.split("#", 1)
            remark = urllib.parse.unquote(remark)

        if remark_override:
            remark = remark_override

        params = {}
        if "?" in main_part:
            main_part, query_str = main_part.split("?", 1)
            params = dict(urllib.parse.parse_qsl(query_str))

        if "@" in main_part:
            user_info, host_port = main_part.split("@", 1)
            uuid = user_info
        else:
            return None

        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
        else:
            host = host_port
            port = 443

        final_link = link
        if remark_override:
            if "#" in final_link:
                final_link = final_link.split("#")[0]
            final_link = f"{final_link}#{urllib.parse.quote(remark)}"

        node = {
            "id": uuid,
            "remark": remark,
            "port": int(port),
            "protocol": "vless",
            "settings": {
                "clients": [{"id": uuid, "flow": params.get("flow", "")}],
                "decryption": "none",
            },
            "streamSettings": {
                "network": params.get("type", "tcp"),
                "security": params.get("security", "none"),
                "xhttpSettings": {
                    "path": params.get("path", ""),
                    "mode": params.get("mode", "auto"),
                    "host": params.get("host", ""),
                },
                "realitySettings": {
                    "serverName": params.get("sni", ""),
                    "shortId": params.get("sid", ""),
                    "publicKey": params.get("pbk", ""),
                },
            },
            "enable": True,
            "_is_custom": True,
            "_raw_link": final_link,
        }
        return node

    except Exception as e:
        logger.warning(f"⚠️ 解析 VLESS 链接失败: {e}")
        return None


def safe_base64(s):
    return base64.urlsafe_b64encode(s.encode('utf-8')).decode('utf-8')


def decode_base64_safe(s):
    try:
        missing_padding = len(s) % 4
        if missing_padding:
            s += '=' * (4 - missing_padding)
        return base64.urlsafe_b64decode(s).decode('utf-8')
    except:
        try:
            return base64.b64decode(s).decode('utf-8')
        except:
            return ""


def generate_node_link(node, server_host):
    try:
        address = node.get('listen') or _clean_server_host(server_host)
        protocol = node.get('protocol')

        if protocol == 'vmess':
            return _build_vmess_link(node, address)
        if protocol == 'vless':
            return _build_vless_link(node, address)
        if protocol == 'trojan':
            return _build_trojan_link(node, address)
        if protocol == 'shadowsocks':
            return _build_shadowsocks_link(node, address)

    except Exception:
        return ""
    return ""



def _surge_line_from_vmess_link(raw_link, remark, node):
    """从 vmess:// 分享链接生成 Surge 配置行。"""
    import json

    try:
        payload_b64 = raw_link[len('vmess://'):]
        payload = json.loads(decode_base64_safe(payload_b64))
        if not isinstance(payload, dict):
            return ''

        v_host = str(payload.get('add', '')).strip()
        v_port = str(payload.get('port', '')).strip()
        v_uuid = str(payload.get('id', '')).strip()
        v_net = str(payload.get('net', 'tcp')).strip()
        v_tls = str(payload.get('tls', '')).strip()
        v_sni = str(payload.get('sni', '')).strip()
        v_host_header = str(payload.get('host', '')).strip()
        v_path = str(payload.get('path', '/')).strip()

        if not v_host or not v_port or not v_uuid:
            return ''

        line = f"{remark} = vmess, {v_host}, {v_port}, username={v_uuid}, vmess-aead=true"

        if v_net == 'ws':
            line += f", ws=true, ws-path={v_path}"
            if v_host_header:
                line += f", ws-headers=Host:{v_host_header}"

        if v_tls in ('tls',):
            line += ", tls=true"
            if v_sni:
                line += f", sni={v_sni}"
            line += ", skip-cert-verify=true"

        line += ", tfo=true, udp-relay=true"
        return _append_underlying_proxy(line, node)
    except Exception as e:
        return f"// Config Error (vmess link): {e}"


def _surge_line_from_vless_link(raw_link, remark, node):
    """从 vless:// 分享链接生成 Surge 配置行。Surge 5+ 支持 VLESS。"""
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(raw_link)
        v_uuid = parsed.username or ''
        v_host = parsed.hostname or ''
        v_port = str(parsed.port or '443')

        params = parse_qs(parsed.query)
        v_type = params.get('type', ['tcp'])[0]
        v_security = params.get('security', ['none'])[0]
        v_sni = params.get('sni', [''])[0]
        v_host_header = params.get('host', [''])[0]
        v_path = params.get('path', ['/'])[0]
        v_flow = params.get('flow', [''])[0]
        v_pbk = params.get('pbk', [''])[0]
        v_sid = params.get('sid', [''])[0]

        if not v_host or not v_uuid:
            return ''

        # Surge 不支持 XHTTP 传输
        if v_type == 'xhttp':
            return f"// Surge 暂未原生支持 XHTTP: {remark}"

        line = f"{remark} = vless, {v_host}, {v_port}, username={v_uuid}"

        if v_type == 'ws':
            line += f", ws=true, ws-path={v_path}"
            if v_host_header:
                line += f", ws-headers=Host:{v_host_header}"
        elif v_type == 'grpc':
            svc = params.get('serviceName', [''])[0]
            if svc:
                line += f", grpc=true, grpc-service-name={svc}"

        if v_security == 'tls':
            line += ", tls=true"
            if v_sni:
                line += f", sni={v_sni}"
            line += ", skip-cert-verify=true"
        elif v_security == 'reality':
            line += ", tls=true"
            if v_sni:
                line += f", sni={v_sni}"
            if v_pbk:
                line += f", reality-public-key={v_pbk}"
            if v_sid:
                line += f", reality-short-id={v_sid}"
            line += ", skip-cert-verify=true"

        if v_flow:
            line += f", flow={v_flow}"

        line += ", tfo=true, udp-relay=true"
        return _append_underlying_proxy(line, node)
    except Exception as e:
        return f"// Config Error (vless link): {e}"


def _surge_line_from_trojan_link(raw_link, remark, node):
    """从 trojan:// 分享链接生成 Surge 配置行。"""
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(raw_link)
        t_password = parsed.username or ''
        t_host = parsed.hostname or ''
        t_port = str(parsed.port or '443')

        params = parse_qs(parsed.query)
        t_sni = params.get('sni', [''])[0]
        t_type = params.get('type', ['tcp'])[0]
        t_host_header = params.get('host', [''])[0]
        t_path = params.get('path', ['/'])[0]

        if not t_host or not t_password:
            return ''

        line = f"{remark} = trojan, {t_host}, {t_port}, password={t_password}"

        if t_type == 'ws':
            line += f", ws=true, ws-path={t_path}"
            if t_host_header:
                line += f", ws-headers=Host:{t_host_header}"

        line += ", tls=true"
        if t_sni:
            line += f", sni={t_sni}"
        line += ", skip-cert-verify=true"

        line += ", tfo=true, udp-relay=true"
        return _append_underlying_proxy(line, node)
    except Exception as e:
        return f"// Config Error (trojan link): {e}"


def _surge_line_from_ss_link(raw_link, remark, node):
    """从 ss:// 分享链接生成 Surge 配置行。"""
    try:
        link_body = raw_link[len('ss://'):]
        if '#' in link_body:
            link_body = link_body.split('#', 1)[0]

        if '@' in link_body:
            user_info_b64, server_part = link_body.rsplit('@', 1)
            user_info = decode_base64_safe(user_info_b64)
            if ':' not in user_info:
                return ''
            method, password = user_info.split(':', 1)
            if ':' not in server_part:
                return ''
            ss_host, ss_port = server_part.rsplit(':', 1)
        else:
            decoded = decode_base64_safe(link_body)
            if '@' not in decoded:
                return ''
            user_info, server_part = decoded.rsplit('@', 1)
            if ':' not in user_info or ':' not in server_part:
                return ''
            method, password = user_info.split(':', 1)
            ss_host, ss_port = server_part.rsplit(':', 1)

        if not ss_host or not ss_port or not method or not password:
            return ''

        line = f"{remark} = ss, {ss_host}, {ss_port}, encrypt-method={method}, password={password}"
        line += ", tfo=true, udp-relay=true"
        return _append_underlying_proxy(line, node)
    except Exception as e:
        return f"// Config Error (ss link): {e}"


def generate_detail_config(node, server_host):
    try:
        clean_host = _clean_server_host(server_host)
        remark = node.get('remark', 'Unnamed').replace(',', '_').replace('=', '_').strip()
        address = node.get('listen') or clean_host
        port = node.get('port', '')

        # 自定义节点（_is_custom）和独立节点都带 _raw_link，统一从链接解析生成 Surge 行。
        # 独立节点的数据结构只有 {id, remark, _raw_link, enable}，没有 _is_custom/protocol 等字段，
        # 所以必须靠 _raw_link 判断而不是 _is_custom。
        raw_link = node.get('_raw_link', '')

        if raw_link:
            if raw_link.startswith('snell://'):
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(raw_link)
                psk = parsed.username
                s_host = parsed.hostname or address
                s_port = parsed.port or port

                params = parse_qs(parsed.query)
                version = params.get('version', ['4'])[0]

                line = f"{remark} = snell, {s_host}, {s_port}, psk={psk}, version={version}, tfo=true, reuse=true"
                return _append_underlying_proxy(line, node)

            elif raw_link.startswith('hy2://'):
                from urllib.parse import parse_qs, urlparse

                # Handle port parsing issues with urlparse when port is a range like "20000-50000"
                # urlparse will raise ValueError if port is not an integer.
                # So we extract the port part manually if it contains '-'
                try:
                    parsed = urlparse(raw_link)
                    password = parsed.username
                    h_host = parsed.hostname or address
                    h_port = parsed.port or port
                except ValueError:
                    # Fallback for ranges
                    # e.g., hy2://password@host:20000-50000?sni=xxx
                    import re
                    match = re.match(r'hy2://([^@]+)@([^:]+):([0-9\-]+)(?:\?(.*))?', raw_link)
                    if match:
                        password = match.group(1)
                        h_host = match.group(2)
                        h_port = match.group(3)
                        query_str = match.group(4) or ''
                        class DummyParsed:
                            query = query_str
                        parsed = DummyParsed()
                    else:
                        raise

                if str(port) and '-' in str(port):
                    h_port = port

                params = parse_qs(parsed.query)
                sni = params.get('sni', [''])[0] or params.get('peer', [''])[0]

                line = f"{remark} = hysteria2, {h_host}, {h_port}, password={password}"
                if str(port) and '-' in str(port) and str(port) == str(h_port):
                     first_port = str(h_port).split('-')[0]
                     last_port = str(h_port).split('-')[1]
                     line = f"{remark} = hysteria2, {h_host}, {first_port}, password={password}, port-hopping={h_port}"

                if sni:
                    line += f", sni={sni}"
                line += ", skip-cert-verify=true, download-bandwidth=1000, udp-relay=true"
                return _append_underlying_proxy(line, node)

            elif raw_link.startswith('vmess://'):
                return _surge_line_from_vmess_link(raw_link, remark, node)

            elif raw_link.startswith('vless://'):
                return _surge_line_from_vless_link(raw_link, remark, node)

            elif raw_link.startswith('trojan://'):
                return _surge_line_from_trojan_link(raw_link, remark, node)

            elif raw_link.startswith('ss://'):
                return _surge_line_from_ss_link(raw_link, remark, node)

        protocol = node.get('protocol')
        settings = _get_settings(node)
        stream = _get_stream_settings(node)
        net = stream.get('network', 'tcp')
        security = stream.get('security', 'none')
        tls = (security == 'tls') or (security == 'reality')

        if protocol == 'vmess':
            clients = settings.get('clients') or [{}]
            uuid = clients[0].get('id', '')
            # 核心修复：强行加上 vmess-aead=true 治愈 Surge 的强迫症
            line = f"{remark} = vmess, {address}, {port}, username={uuid}, vmess-aead=true"
            
            if net == 'ws':
                ws_set = stream.get('wsSettings', {})
                path = ws_set.get('path', '/')
                panel_host = ws_set.get('headers', {}).get('Host', '')
                sni = ""
                if tls:
                    tls_set = stream.get('tlsSettings', {})
                    sni = tls_set.get('serverName', '')
                line += f", ws=true, ws-path={path}"
                if tls and sni:
                    pass
                elif panel_host:
                    line += f", ws-headers=Host:{panel_host}"
            if tls:
                line += ", tls=true"
                tls_set = stream.get('tlsSettings', {})
                sni = tls_set.get('serverName', '')
                if sni:
                    line += f", sni={sni}"
                line += ", skip-cert-verify=true"
            line += ", tfo=true, udp-relay=true"
            return _append_underlying_proxy(line, node)

        elif protocol == 'trojan':
            clients = settings.get('clients') or [{}]
            password = clients[0].get('password', '')
            line = f"{remark} = trojan, {address}, {port}, password={password}"
            if tls:
                line += ", tls=true"
                sni = stream.get('tlsSettings', {}).get('serverName', '')
                if sni:
                    line += f", sni={sni}"
                line += ", skip-cert-verify=true"
            line += ", tfo=true, udp-relay=true"
            return _append_underlying_proxy(line, node)

    except Exception as e:
        return f"// Config Error: {str(e)}"

    return ""


# ============================================================================
# Egern 订阅格式（YAML）
#
# Egern 用的是 YAML：顶层 `proxies:`，每个节点是一个 `- <type>:` 映射，和 Surge 的
# INI 行完全不同。所以这里**本地生成**（不走 subconverter），链路与 Surge 那套对齐：
# generate_egern_proxy 对应 generate_detail_config，都是「优先解析 _raw_link，面板节点
# 退回结构化字段」。区别只在产物——Surge 出字符串行，Egern 出 dict，最后由
# egern_yaml_block 序列化成 YAML。
#
# 不引入 PyYAML（requirements.txt 里没有）：字符串标量能裸写就裸写，不安全的用
# json.dumps 双引号——JSON 字符串本就是合法的 YAML 双引号标量，这也是 Sub-Store 的做法。
# ============================================================================

# 能安全裸写的 YAML 标量：只含字母数字和有限符号，且不撞 YAML 的保留字 / 纯数字
# （纯数字若裸写会被解析成 int，而 name/short_id 这类必须保持字符串）。
_EGERN_SAFE_SCALAR = re.compile(r'[A-Za-z0-9_.\-/@]+')
_EGERN_NUMERIC = re.compile(r'-?\d+(\.\d+)?')
_EGERN_YAML_RESERVED = {'true', 'false', 'yes', 'no', 'null', 'none', 'on', 'off', '~'}

# Egern 的 Shadowsocks 加密名与分享链接里的 v2ray 写法有出入，需要映射。
_EGERN_SS_METHOD_MAP = {
    'chacha20-ietf-poly1305': 'chacha20-poly1305',
    'xchacha20-ietf-poly1305': 'xchacha20-poly1305',
}


def _egern_int(v, default):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _egern_reality(q):
    """从查询参数里抽出 reality 配置（public_key / short_id）。"""
    r = {}
    if q.get('pbk'):
        r['public_key'] = q['pbk']
    if q.get('sid'):
        r['short_id'] = q['sid']
    return r


def _egern_scalar(value):
    """把一个标量渲染成 YAML 值。"""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if (_EGERN_SAFE_SCALAR.fullmatch(s)
            and s.lower() not in _EGERN_YAML_RESERVED
            and not _EGERN_NUMERIC.fullmatch(s)):
        return s
    # JSON 字符串是合法的 YAML 双引号标量，转义交给 json 处理
    return json.dumps(s, ensure_ascii=False)


def _egern_dump(obj, indent):
    """把嵌套 dict 递归渲染成缩进式 YAML（块状），None 值跳过。"""
    pad = '  ' * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if v is None:
                continue
            if isinstance(v, dict):
                inner = {kk: vv for kk, vv in v.items() if vv is not None}
                if not inner:
                    continue
                lines.append(f"{pad}{k}:")
                lines.append(_egern_dump(inner, indent + 1))
            elif isinstance(v, list):
                if not v:
                    continue
                lines.append(f"{pad}{k}:")
                for it in v:
                    if isinstance(it, (dict, list)):
                        lines.append(f"{pad}  - {json.dumps(it, ensure_ascii=False)}")
                    else:
                        lines.append(f"{pad}  - {_egern_scalar(it)}")
            else:
                lines.append(f"{pad}{k}: {_egern_scalar(v)}")
    return '\n'.join(lines)


def egern_yaml_block(proxy):
    """把 {'<type>': {...}} 渲染成 proxies 列表里的一项（含 '  - ' 前缀，末尾换行）。"""
    block = _egern_dump(proxy, 2)  # indent=2 → 顶层键前有 4 个空格
    if not block:
        return ''
    parts = block.split('\n')
    first = parts[0][4:]  # 去掉那 4 个空格，换成 '  - '
    out = f"  - {first}"
    if len(parts) > 1:
        out += '\n' + '\n'.join(parts[1:])
    return out + '\n'


def _egern_from_vless_link(raw_link, remark):
    from urllib.parse import parse_qs, unquote, urlparse

    parsed = urlparse(raw_link)
    uuid = parsed.username or ''
    host = parsed.hostname or ''
    if not host or not uuid:
        return None
    port = parsed.port or 443
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    name = remark or (unquote(parsed.fragment) if parsed.fragment else 'Unnamed')
    net = q.get('type', 'tcp')
    security = q.get('security', 'none')
    sni = q.get('sni') or q.get('host') or ''
    ws_host = q.get('host', '')
    path = q.get('path', '')  # parse_qs 已 url-decode
    flow = q.get('flow', '')
    tls = security in ('tls', 'reality')
    reality = security == 'reality'

    node = {'name': name, 'server': host, 'port': port, 'user_id': uuid, 'udp_relay': True}
    transport = None
    if net == 'ws':
        key = 'wss' if tls else 'ws'
        t = {'path': path or '/'}
        if ws_host:
            t['headers'] = {'Host': ws_host}
        if tls:
            if sni:
                t['sni'] = sni
            t['skip_tls_verify'] = True
        transport = {key: t}
    elif net == 'grpc':
        t = {'service_name': q.get('serviceName', '')}
        if sni:
            t['sni'] = sni
        if reality:
            r = _egern_reality(q)
            if r:
                t['reality'] = r
        if tls:
            t['skip_tls_verify'] = True
        transport = {'grpc': t}
    else:  # tcp（及其它），只有带 tls/reality 才有意义
        if tls:
            t = {}
            if sni:
                t['sni'] = sni
            t['skip_tls_verify'] = True
            if reality:
                r = _egern_reality(q)
                if r:
                    t['reality'] = r
            transport = {'tls': t}
            # flow 只对 vless 顶层、且只认 xtls-rprx-vision
            if flow == 'xtls-rprx-vision':
                node['flow'] = flow
    if transport:
        node['transport'] = transport
    return {'vless': node}


def _egern_from_vmess_link(raw_link, remark):
    payload = json.loads(decode_base64_safe(raw_link[len('vmess://'):]))
    if not isinstance(payload, dict):
        return None
    host = str(payload.get('add', '')).strip()
    uuid = str(payload.get('id', '')).strip()
    if not host or not uuid:
        return None
    port = _egern_int(payload.get('port'), 443)
    net = str(payload.get('net', 'tcp')).strip()
    tls = str(payload.get('tls', '')).strip() in ('tls', 'reality')
    sni = str(payload.get('sni', '')).strip()
    ws_host = str(payload.get('host', '')).strip()
    path = str(payload.get('path', '/')).strip()
    scy = str(payload.get('scy', 'auto')).strip() or 'auto'
    aid = _egern_int(payload.get('aid'), 0)
    name = remark or str(payload.get('ps', '')).strip() or 'Unnamed'

    node = {'name': name, 'server': host, 'port': port, 'user_id': uuid,
            'security': scy, 'udp_relay': True}
    if aid != 0:
        node['legacy'] = True  # 非 0 alterId 走旧版 VMess

    transport = None
    if net == 'ws':
        key = 'wss' if tls else 'ws'
        t = {'path': path or '/'}
        if ws_host:
            t['headers'] = {'Host': ws_host}
        if tls:
            if sni:
                t['sni'] = sni
            t['skip_tls_verify'] = True
        transport = {key: t}
    elif net == 'grpc':
        t = {'service_name': path}  # vmess grpc 的 serviceName 落在 path 字段
        if sni:
            t['sni'] = sni
        if tls:
            t['skip_tls_verify'] = True
        transport = {'grpc': t}
    else:  # tcp
        if tls:
            t = {}
            if sni:
                t['sni'] = sni
            t['skip_tls_verify'] = True
            transport = {'tls': t}
    if transport:
        node['transport'] = transport
    return {'vmess': node}


def _egern_from_trojan_link(raw_link, remark):
    from urllib.parse import parse_qs, unquote, urlparse

    parsed = urlparse(raw_link)
    password = parsed.username or ''
    host = parsed.hostname or ''
    if not host or not password:
        return None
    port = parsed.port or 443
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    name = remark or (unquote(parsed.fragment) if parsed.fragment else 'Unnamed')
    sni = q.get('sni') or q.get('peer') or host
    net = q.get('type', 'tcp')
    ws_host = q.get('host', '')
    path = q.get('path', '/')

    # Trojan 恒为 TLS；skip_tls_verify 与面板 Surge 生成保持一致（恒 true）
    node = {'name': name, 'server': host, 'port': port, 'password': password,
            'udp_relay': True, 'sni': sni, 'skip_tls_verify': True}
    if net == 'ws':
        # Egern 的 trojan-over-ws 用顶层 websocket，不是 transport
        ws = {'path': path or '/'}
        if ws_host:
            ws['host'] = ws_host
        node['websocket'] = ws
    if q.get('security') == 'reality':
        r = _egern_reality(q)
        if r:
            node['reality'] = r
    return {'trojan': node}


def _egern_from_ss_link(raw_link, remark):
    from urllib.parse import unquote

    body = raw_link[len('ss://'):]
    frag_name = ''
    if '#' in body:
        body, frag = body.split('#', 1)
        frag_name = unquote(frag)
    if '?' in body:
        body = body.split('?', 1)[0]

    if '@' in body:
        cred_b64, server_part = body.rsplit('@', 1)
        cred = decode_base64_safe(cred_b64)
    else:
        decoded = decode_base64_safe(body)
        if '@' not in decoded:
            return None
        cred, server_part = decoded.rsplit('@', 1)
    if ':' not in cred or ':' not in server_part:
        return None
    method, password = cred.split(':', 1)
    host, port = server_part.rsplit(':', 1)
    if not host or not port:
        return None

    name = remark or frag_name or 'Unnamed'
    method = _EGERN_SS_METHOD_MAP.get(method, method)
    node = {'name': name, 'method': method, 'server': host,
            'port': _egern_int(port, 0), 'password': password, 'udp_relay': True}
    return {'shadowsocks': node}


def _egern_from_hy2_link(raw_link, remark):
    from urllib.parse import parse_qs, unquote

    # 不用 urlparse：端口可能是 20000-50000 这种段，urlparse 取 .port 会抛错
    body = raw_link.split('://', 1)[1] if '://' in raw_link else raw_link
    frag_name = ''
    if '#' in body:
        body, frag = body.split('#', 1)
        frag_name = unquote(frag)
    query = ''
    if '?' in body:
        body, query = body.split('?', 1)
    if '@' not in body:
        return None
    password, hostport = body.split('@', 1)
    if ':' in hostport:
        host, port_raw = hostport.rsplit(':', 1)
    else:
        host, port_raw = hostport, '443'
    if not host or not password:
        return None

    q = {k: v[0] for k, v in parse_qs(query).items()}
    name = remark or frag_name or 'Unnamed'
    sni = q.get('sni') or q.get('peer') or ''

    node = {'name': name, 'server': host}
    if '-' in port_raw:
        node['port'] = _egern_int(port_raw.split('-')[0], 443)
        node['port_hopping'] = port_raw
    else:
        node['port'] = _egern_int(port_raw, 443)
    node['auth'] = password
    node['udp_relay'] = True
    if sni:
        node['sni'] = sni
    node['skip_tls_verify'] = True
    if q.get('obfs') == 'salamander' and q.get('obfs-password'):
        node['obfs'] = 'salamander'
        node['obfs_password'] = q.get('obfs-password')
    return {'hysteria2': node}


def _egern_from_snell_link(raw_link, remark):
    from urllib.parse import parse_qs, unquote, urlparse

    parsed = urlparse(raw_link)
    psk = parsed.username or ''
    host = parsed.hostname or ''
    if not host or not psk:
        return None
    port = parsed.port or 0
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    name = remark or (unquote(parsed.fragment) if parsed.fragment else 'Unnamed')
    version = _egern_int(q.get('version', '4'), 4)
    node = {'name': name, 'server': host, 'port': port, 'psk': psk,
            'version': version, 'udp_relay': True}
    return {'snell': node}


def generate_egern_proxy(node, server_host):
    """把一个节点转成 Egern proxy 字典 {'<type>': {...}}；转不了返回 None。

    与 generate_detail_config（Surge）同构：独立节点 / 自定义节点带 _raw_link，直接从
    链接解析；面板节点没有 _raw_link，先用 generate_node_link 拼出分享链接再走同一套
    解析器——这样就不用再为「结构化字段 → Egern」单独写一遍映射。
    """
    try:
        remark = str(node.get('remark') or '').strip() or 'Unnamed'
        raw_link = str(node.get('_raw_link') or '').strip()
        if not raw_link:
            raw_link = generate_node_link(node, server_host) or ''
        if not raw_link:
            return None

        if raw_link.startswith('vless://'):
            return _egern_from_vless_link(raw_link, remark)
        if raw_link.startswith('vmess://'):
            return _egern_from_vmess_link(raw_link, remark)
        if raw_link.startswith('trojan://'):
            return _egern_from_trojan_link(raw_link, remark)
        if raw_link.startswith('ss://'):
            return _egern_from_ss_link(raw_link, remark)
        if raw_link.startswith('hy2://') or raw_link.startswith('hysteria2://'):
            return _egern_from_hy2_link(raw_link, remark)
        if raw_link.startswith('snell://'):
            return _egern_from_snell_link(raw_link, remark)
        return None
    except Exception as e:
        logger.warning(f"⚠️ [Egern] 生成节点失败 ({node.get('remark', '?')}): {e}")
        return None
