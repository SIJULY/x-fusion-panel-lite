import asyncio
import time

import httpx

from app.core.state import ADMIN_CONFIG

# 进程级共享的 httpx 客户端，按 (事件循环, token, email) 索引。
#
# 以前每个 CF 方法结尾都是 `finally: await self.close()`：调一次 API 就把连接池
# 连同 TLS 会话一起丢掉，下一次从 TCP 握手重来。实测「打开详情页」那一串调用里，
# 同一方法内连续两个请求间隔 258-295ms，而跨方法要 615-968ms —— 差出来的
# 350-700ms 全是白扔的握手。域名解析改成交互式之后，这笔开销会直接变成用户
# 打开页面时的等待，所以必须复用。
#
# 键里带事件循环 id：httpx 的连接池绑在创建它的 loop 上，换了 loop（单测里
# 每个 asyncio.run 都是新的）必须重建，否则复用会拿到已经废掉的连接。
_CLIENTS = {}

# Zone 列表缓存，按 (token, email) 索引，值是 (时间戳, zones)。
#
# get_zone_id 无条件先调 list_zones()，而打开一次详情页会经由
# load_cloudflare_records / list_a_records_by_ip / get_a_record_ip_by_domain
# 反复走到这里。zone 列表是账号级的、几乎不变，缓存 10 分钟足够；
# 设置页那个「刷新域名」按钮走 use_cache=False，用户手点时永远拿实时的。
_ZONE_CACHE = {}
_ZONE_CACHE_TTL = 600


def invalidate_cf_cache():
    """token / 根域名改动后调用：清掉 zone 缓存并弃用旧客户端。

    换了 token 就是换了账号，能看到的 zone 完全不同，缓存必须作废。
    旧客户端的 aclose 是 async 的，这里只能丢引用 + 尽力异步关闭；
    丢掉引用之后它不会再被任何调用取到，剩下的连接由 httpx 自己回收。
    """
    _ZONE_CACHE.clear()
    stale = list(_CLIENTS.values())
    _CLIENTS.clear()
    for c in stale:
        try:
            if not c.is_closed:
                asyncio.get_running_loop().create_task(c.aclose())
        except Exception:
            pass


class CloudflareHandler:
    def __init__(self):
        self.token = ADMIN_CONFIG.get('cf_api_token', '')
        self.email = ADMIN_CONFIG.get('cf_email', '')
        self.root_domain = ADMIN_CONFIG.get('cf_root_domain', '')
        self.base_url = "https://api.cloudflare.com/client/v4"

    def _cache_key(self):
        return (self.token, self.email)

    @property
    def client(self):
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = 0
        key = (loop_id,) + self._cache_key()
        c = _CLIENTS.get(key)
        if c is None or c.is_closed:
            c = httpx.AsyncClient(headers=self._headers(), timeout=15.0)
            _CLIENTS[key] = c
        return c

    async def close(self):
        """关掉本 token 对应的共享客户端。

        **单次 API 调用不要调它** —— 那正是以前每次都重新握手的原因。
        只有换 token（invalidate_cf_cache）或进程收尾时才需要。
        """
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = 0
        c = _CLIENTS.pop((loop_id,) + self._cache_key(), None)
        if c is not None and not c.is_closed:
            try:
                await c.aclose()
            except Exception:
                pass

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.email and "global" in self.token.lower():
            h["X-Auth-Email"] = self.email
            h["X-Auth-Key"] = self.token
        else:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def get_zone_id(self, domain_name=None):
        target = str(domain_name or self.root_domain or '').strip().lower().rstrip('.')
        if not target:
            return None, "未指定 Zone 域名"

        try:
            ok, zones = await self.list_zones()
            if ok:
                normalized = []
                for item in zones or []:
                    name = str(item.get('name', '')).strip().lower().rstrip('.')
                    zone_id = str(item.get('id', '')).strip()
                    if name and zone_id:
                        normalized.append((name, zone_id))

                for zone_name, zone_id in normalized:
                    if zone_name == target:
                        return zone_id, None

                matched = [(zone_name, zone_id) for zone_name, zone_id in normalized
                           if target == zone_name or target.endswith(f'.{zone_name}')]
                if matched:
                    zone_name, zone_id = max(matched, key=lambda x: len(x[0]))
                    return zone_id, None

            url = f"{self.base_url}/zones?name={target}"
            r = await self.client.get(url)
            data = r.json()
            if data.get('success') and len(data['result']) > 0:
                return data['result'][0]['id'], None
            return None, f"未找到 Zone: {target}"
        except Exception as e:
            return None, str(e)

    async def list_zones(self, use_cache=True):
        if not self.token:
            return False, "未配置 Cloudflare Token"

        key = self._cache_key()
        if use_cache:
            hit = _ZONE_CACHE.get(key)
            if hit and time.time() - hit[0] < _ZONE_CACHE_TTL:
                # 复制一层，别让调用方改到缓存里的列表
                return True, list(hit[1])

        url = f"{self.base_url}/zones?per_page=100"
        try:
            r = await self.client.get(url)
            data = r.json()
            if not data.get('success'):
                return False, f"查询 Zone 失败: {data}"

            zones = []
            for item in data.get('result', []) or []:
                name = item.get('name', '').strip()
                zone_id = item.get('id', '').strip()
                if name and zone_id:
                    zones.append({'id': zone_id, 'name': name})

            zones.sort(key=lambda x: x.get('name', ''))
            _ZONE_CACHE[key] = (time.time(), list(zones))
            return True, zones
        except Exception as e:
            return False, str(e)

    def _ensure_fqdn(self, record_name, zone_name):
        record_name = str(record_name or '').strip()
        zone_name = str(zone_name or '').strip()
        if not record_name or not zone_name:
            return ''
        if record_name == '@':
            return zone_name
        if record_name.endswith(f'.{zone_name}') or record_name == zone_name:
            return record_name
        return f"{record_name}.{zone_name}"

    async def set_ssl_flexible(self, zone_id):
        url = f"{self.base_url}/zones/{zone_id}/settings/ssl"
        try:
            payload = {"value": "flexible"}
            r = await self.client.patch(url, json=payload)
            if r.json().get('success'):
                return True, "SSL 已强制设为 Flexible"
            return True, "SSL 设置指令已发送"
        except Exception as e:
            return False, str(e)

    async def auto_configure(self, ip, sub_prefix):
        if not self.token:
            return False, "未配置 API Token"

        try:
            zone_id, err = await self.get_zone_id()
            if not zone_id:
                return False, err

            await self.set_ssl_flexible(zone_id)

            full_domain = f"{sub_prefix}.{self.root_domain}"
            url = f"{self.base_url}/zones/{zone_id}/dns_records"
            payload = {"type": "A", "name": full_domain, "content": ip, "ttl": 1, "proxied": True}
            r = await self.client.post(url, json=payload)
            if r.json().get('success'):
                return True, f"解析成功: {full_domain}"
            else:
                return False, f"CF API 报错: {r.text}"
        except Exception as e:
            return False, str(e)

    async def get_a_record_ip_by_domain(self, domain):
        if not self.token or not domain:
            return False, "未配置 Token 或域名为空"
        
        try:
            zone_id, err = await self.get_zone_id(domain)
            if not zone_id:
                return False, f"找不到 Zone: {err}"
            
            search_url = f"{self.base_url}/zones/{zone_id}/dns_records?name={domain}&type=A"
            r = await self.client.get(search_url)
            data = r.json()
            if not data.get('success'):
                return False, "查询记录失败"
            
            records = data.get('result', [])
            if records:
                return True, records[0].get('content')
            return False, "未找到 A 记录"
        except Exception as e:
            return False, str(e)

    async def list_a_records_by_ip(self, ip):
        if not self.token:
            return False, "未配置 Cloudflare Token"
        if not ip:
            return False, "IP 为空"

        try:
            ok, zones = await self.list_zones()
            if not ok:
                if not self.root_domain:
                    return False, zones
                zone_id, err = await self.get_zone_id()
                if not zone_id:
                    return False, err
                zones = [{'id': zone_id, 'name': self.root_domain}]

            matched = []

            for zone in zones:
                zone_id = zone.get('id', '')
                zone_name = zone.get('name', '')
                if not zone_id:
                    continue

                page = 1
                per_page = 100
                while True:
                    search_url = (
                        f"{self.base_url}/zones/{zone_id}/dns_records"
                        f"?type=A&content={ip}&page={page}&per_page={per_page}"
                    )
                    r = await self.client.get(search_url)
                    data = r.json()
                    if not data.get('success'):
                        return False, f"查询记录失败: {data}"

                    result = data.get('result', []) or []
                    for rec in result:
                        matched.append({
                            'id': rec.get('id', ''),
                            'zone_id': zone_id,
                            'zone_name': zone_name,
                            'name': rec.get('name', ''),
                            'type': rec.get('type', 'A'),
                            'content': rec.get('content', ''),
                            'proxied': bool(rec.get('proxied', False)),
                            'ttl': rec.get('ttl', 1),
                        })

                    result_info = data.get('result_info', {}) or {}
                    total_pages = int(result_info.get('total_pages') or 1)
                    if page >= total_pages:
                        break
                    page += 1

            matched.sort(key=lambda x: (x.get('zone_name', ''), x.get('name', '')))
            return True, matched
        except Exception as e:
            return False, str(e)

    async def list_all_a_records(self):
        if not self.token:
            return False, "未配置 Cloudflare Token"

        try:
            ok, zones = await self.list_zones()
            if not ok:
                if not self.root_domain:
                    return False, zones
                zone_id, err = await self.get_zone_id()
                if not zone_id:
                    return False, err
                zones = [{'id': zone_id, 'name': self.root_domain}]

            records = []
            for zone in zones:
                zone_id = zone.get('id', '')
                zone_name = zone.get('name', '')
                if not zone_id:
                    continue

                page = 1
                per_page = 100
                while True:
                    search_url = (
                        f"{self.base_url}/zones/{zone_id}/dns_records"
                        f"?type=A&page={page}&per_page={per_page}"
                    )
                    r = await self.client.get(search_url)
                    data = r.json()
                    if not data.get('success'):
                        return False, f"查询记录失败: {data}"

                    for rec in data.get('result', []) or []:
                        records.append({
                            'id': rec.get('id', ''),
                            'zone_id': zone_id,
                            'zone_name': zone_name,
                            'name': rec.get('name', ''),
                            'type': rec.get('type', 'A'),
                            'content': rec.get('content', ''),
                            'proxied': bool(rec.get('proxied', False)),
                            'ttl': rec.get('ttl', 1),
                        })

                    result_info = data.get('result_info', {}) or {}
                    total_pages = int(result_info.get('total_pages') or 1)
                    if page >= total_pages:
                        break
                    page += 1

            records.sort(key=lambda x: (x.get('content', ''), x.get('zone_name', ''), x.get('name', '')))
            return True, records
        except Exception as e:
            return False, str(e)

    async def create_a_record(self, record_name, zone_name, ip, proxied=False):
        if not self.token:
            return False, "未配置 Cloudflare Token"
        if not record_name:
            return False, "记录名称不能为空"
        if not zone_name:
            return False, "域名不能为空"
        if not ip:
            return False, "IP 不能为空"

        try:
            zone_id, err = await self.get_zone_id(zone_name)
            if not zone_id:
                return False, err

            full_domain = self._ensure_fqdn(record_name, zone_name)
            url = f"{self.base_url}/zones/{zone_id}/dns_records"
            payload = {
                "type": "A",
                "name": full_domain,
                "content": ip,
                "ttl": 1,
                "proxied": bool(proxied),
            }
            r = await self.client.post(url, json=payload)
            data = r.json()
            if data.get('success'):
                return True, data.get('result', {})
            return False, f"CF API 报错: {data}"
        except Exception as e:
            return False, str(e)

    async def update_a_record(self, record_id, record_name, zone_name, ip, proxied=False):
        if not self.token:
            return False, "未配置 Cloudflare Token"
        if not record_id:
            return False, "记录 ID 不能为空"
        if not record_name:
            return False, "记录名称不能为空"
        if not zone_name:
            return False, "域名不能为空"
        if not ip:
            return False, "IP 不能为空"

        try:
            zone_id, err = await self.get_zone_id(zone_name)
            if not zone_id:
                return False, err

            full_domain = self._ensure_fqdn(record_name, zone_name)
            url = f"{self.base_url}/zones/{zone_id}/dns_records/{record_id}"
            payload = {
                "type": "A",
                "name": full_domain,
                "content": ip,
                "ttl": 1,
                "proxied": bool(proxied),
            }
            r = await self.client.put(url, json=payload)
            data = r.json()
            if data.get('success'):
                return True, data.get('result', {})
            return False, f"CF API 报错: {data}"
        except Exception as e:
            return False, str(e)

    async def delete_record_by_id(self, record_id, zone_name):
        if not self.token:
            return False, "未配置 Cloudflare Token"
        if not record_id:
            return False, "记录 ID 不能为空"
        if not zone_name:
            return False, "域名不能为空"

        try:
            zone_id, err = await self.get_zone_id(zone_name)
            if not zone_id:
                return False, err

            del_url = f"{self.base_url}/zones/{zone_id}/dns_records/{record_id}"
            r = await self.client.delete(del_url)
            data = r.json()
            if data.get('success'):
                return True, "删除成功"
            return False, f"删除失败: {data}"
        except Exception as e:
            return False, str(e)

    async def delete_record_by_domain(self, domain_to_delete):
        if not self.token:
            return False, "未配置 Cloudflare Token"
        if not domain_to_delete:
            return False, "域名为空"

        if self.root_domain not in domain_to_delete:
            return False, f"安全拦截: {domain_to_delete} 不属于根域名 {self.root_domain}"

        try:
            zone_id, err = await self.get_zone_id(domain_to_delete)
            if not zone_id:
                return False, f"找不到 Zone: {err}"

            search_url = f"{self.base_url}/zones/{zone_id}/dns_records?name={domain_to_delete}"
            r = await self.client.get(search_url)
            data = r.json()
            if not data.get('success'):
                return False, "查询记录失败"

            records = data.get('result', [])
            if not records:
                return True, "记录不存在，无需删除"

            deleted_count = 0
            for rec in records:
                rec_id = rec['id']
                del_url = f"{self.base_url}/zones/{zone_id}/dns_records/{rec_id}"
                await self.client.delete(del_url)
                deleted_count += 1

            return True, f"已清理 {deleted_count} 条 DNS 记录"

        except Exception as e:
            return False, str(e)