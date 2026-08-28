from app.core.logging import logger
from app.core.state import NODES_DATA, PROBE_DATA_CACHE, SERVERS_CACHE, SUBS_CACHE
from app.services.probe import probe_offline_after
from app.utils.geo import detect_country_group


def format_total_traffic(total_traffic_bytes: int) -> str:
    gb_value = total_traffic_bytes / (1024**3)
    if gb_value >= 1024:
        return f"{gb_value / 1024:.2f} T"
    return f"{gb_value:.2f} GB"


def get_dashboard_live_data():
    data = calculate_dashboard_data()
    return data if data else {"error": "Calculation failed"}


def calculate_dashboard_data():
    """
    计算并返回当前所有面板数据。
    逻辑调整：优先使用 Root 探针的流量和状态，没有探针才使用 X-UI 数据。
    """
    try:
        total_servers = len(SERVERS_CACHE)
        logger.debug(f"[DashboardCalc] start | total_servers={total_servers} nodes_cache_keys={len(NODES_DATA)} probe_cache_keys={len(PROBE_DATA_CACHE)} subs={len(SUBS_CACHE)}")
        online_servers = 0
        total_nodes = 0
        total_traffic_bytes = 0

        server_traffic_map = {}
        from collections import Counter
        country_counter = Counter()

        import time
        now_ts = time.time()
        offline_after = probe_offline_after()

        for s in SERVERS_CACHE:
            res = NODES_DATA.get(s['url'], []) or []
            custom = s.get('custom_nodes', []) or []
            probe_data = PROBE_DATA_CACHE.get(s['url'])

            name = s.get('name', '未命名')

            try:
                region_str = detect_country_group(name, s)
                if not region_str or region_str.strip() == "🏳️":
                    region_str = "🏳️ 未知区域"
            except:
                region_str = "🏳️ 未知区域"
            country_counter[region_str] += 1

            srv_traffic = 0
            use_probe_traffic = False

            if s.get('probe_installed') and probe_data:
                t_in = probe_data.get('net_total_in', 0)
                t_out = probe_data.get('net_total_out', 0)
                if t_in > 0 or t_out > 0:
                    srv_traffic = t_in + t_out
                    use_probe_traffic = True

            if not use_probe_traffic and res:
                for n in res:
                    srv_traffic += int(n.get('up', 0)) + int(n.get('down', 0))

            total_traffic_bytes += srv_traffic
            server_traffic_map[name] = srv_traffic

            is_online = False

            if s.get('probe_installed') and probe_data:
                if now_ts - probe_data.get('last_updated', 0) < offline_after:
                    is_online = True

            if not is_online:
                if res or s.get('_status') == 'online':
                    is_online = True

            if is_online:
                online_servers += 1

            if res:
                total_nodes += len(res)
            if custom:
                total_nodes += len(custom)

        sorted_traffic = sorted(server_traffic_map.items(), key=lambda x: x[1], reverse=True)[:15]
        bar_names = [x[0] for x in sorted_traffic]
        bar_values = [round(x[1] / (1024**3), 2) for x in sorted_traffic]

        chart_data = []
        sorted_regions = country_counter.most_common()

        if len(sorted_regions) > 5:
            top_5 = sorted_regions[:5]
            others_count = sum(item[1] for item in sorted_regions[5:])
            for k, v in top_5:
                chart_data.append({'name': f"{k} ({v})", 'value': v})
            if others_count > 0:
                chart_data.append({'name': f"🏳️ 其他 ({others_count})", 'value': others_count})
        else:
            for k, v in sorted_regions:
                chart_data.append({'name': f"{k} ({v})", 'value': v})

        if not chart_data:
            chart_data = [{'name': '暂无数据', 'value': 0}]

        result = {
            "servers": f"{online_servers}/{total_servers}",
            "nodes": str(total_nodes),
            "traffic": format_total_traffic(total_traffic_bytes),
            "subs": str(len(SUBS_CACHE)),
            "bar_chart": {"names": bar_names, "values": bar_values},
            "pie_chart": chart_data
        }
        logger.debug(f"[DashboardCalc] result={result}")
        return result
    except Exception as e:
        logger.exception(f"仪表盘数据计算失败: {e}")
        return None
