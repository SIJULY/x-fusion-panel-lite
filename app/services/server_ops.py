import asyncio

from nicegui import run

from app.core.config import AUTO_COUNTRY_MAP
from app.core.logging import logger
from app.core.state import SERVERS_CACHE
from app.services.manager_factory import get_manager
from app.storage.repositories import save_servers
from app.utils.geo import fetch_geo_from_ip, get_flag_for_country


async def force_geoip_naming_task(server_conf, max_retries=10):
    """
    强制执行 GeoIP 解析，直到成功或达到最大重试次数。
    成功后：
    1. 命名格式：🇺🇸 美国-1, 🇭🇰 香港-2
    2. 分组：自动分入对应国家组
    """
    url = server_conf['url']
    logger.info(f"🌍 [强制修正] 开始处理: {url} (目标: 国旗+国家+序号)")

    for i in range(max_retries):
        try:
            geo_info = await run.io_bound(fetch_geo_from_ip, url)

            if geo_info:
                country_raw = geo_info[2]
                flag_group = get_flag_for_country(country_raw)

                count = 1
                for s in SERVERS_CACHE:
                    if s is not server_conf and s.get('name', '').startswith(flag_group):
                        count += 1

                final_name = f"{flag_group}-{count}"

                old_name = server_conf.get('name', '')
                if old_name != final_name:
                    server_conf['name'] = final_name
                    server_conf['group'] = flag_group
                    server_conf['_detected_region'] = country_raw

                    await save_servers()

                    logger.info(f"✅ [强制修正] 成功: {old_name} -> {final_name} (第 {i+1} 次尝试)")
                    return

            logger.warning(f"⏳ [强制修正] 第 {i+1} 次解析 IP 归属地失败，3秒后重试...")

        except Exception as e:
            logger.error(f"❌ [强制修正] 异常: {e}")

        await asyncio.sleep(3)

    logger.warning(f"⚠️ [强制修正] 最终失败: 达到最大重试次数，保持原名 {server_conf.get('name')}")


async def generate_smart_name(server_conf):
    """尝试获取面板节点名，获取不到则用 GeoIP+序号"""
    try:
        mgr = get_manager(server_conf)
        inbounds = await mgr.get_inbounds()
        if inbounds and len(inbounds) > 0:
            for node in inbounds:
                if node.get('remark'):
                    return node['remark']
    except:
        pass

    try:
        geo_info = await run.io_bound(fetch_geo_from_ip, server_conf['url'])
        if geo_info:
            country_name = geo_info[2]
            flag_prefix = get_flag_for_country(country_name)

            count = 1
            for s in SERVERS_CACHE:
                if s.get('name', '').startswith(flag_prefix):
                    count += 1
            return f"{flag_prefix}-{count}"
    except:
        pass

    return f"Server-{len(SERVERS_CACHE) + 1}"


async def fast_resolve_single_server(s):
    """
    后台全自动修正流程：
    1. 尝试连接面板，读取第一个节点的备注名 (Smart Name)
    2. 尝试查询 IP 归属地，获取国旗 (GeoIP)
    3. 自动组合名字 (防止国旗重复)
    4. 自动归类分组
    """
    await asyncio.sleep(1.5)

    raw_ip = s['url'].split('://')[-1].split(':')[0]
    logger.info(f"🔍 [智能修正] 正在处理: {raw_ip} ...")

    data_changed = False

    try:
        current_pure_name = s['name'].replace('🏳️', '').strip()

        if current_pure_name == raw_ip:
            try:
                smart_name = await generate_smart_name(s)
                if smart_name and smart_name != raw_ip and not smart_name.startswith('Server-'):
                    s['name'] = smart_name
                    data_changed = True
                    logger.info(f"🏷️ [获取备注] 成功: {smart_name}")
            except Exception as e:
                logger.warning(f"⚠️ [获取备注] 失败: {e}")

        geo = await run.io_bound(fetch_geo_from_ip, s['url'])

        if geo:
            country_name = geo[2]
            s['lat'] = geo[0]
            s['lon'] = geo[1]
            s['_detected_region'] = country_name

            flag_group = get_flag_for_country(country_name)
            flag_icon = flag_group.split(' ')[0]

            temp_name = s['name'].replace('🏳️', '').strip()

            if flag_icon in temp_name:
                if s['name'] != temp_name:
                    s['name'] = temp_name
                    data_changed = True
            else:
                s['name'] = f"{flag_icon} {temp_name}"
                data_changed = True

            target_group = flag_group

            for k, v in AUTO_COUNTRY_MAP.items():
                if flag_icon in k or flag_icon in v:
                    target_group = v
                    break

            if s.get('group') != target_group:
                s['group'] = target_group
                data_changed = True

        else:
            logger.warning(f"⚠️ [GeoIP] 未获取到地理位置: {raw_ip}")

        if data_changed:
            await save_servers()
            logger.info(f"✅ [智能修正] 完毕: {s['name']} -> [{s['group']}]")

    except Exception as e:
        logger.error(f"❌ [智能修正] 严重错误: {e}")


def get_all_groups():
    groups = {'默认分组', '自动注册'}
    for s in SERVERS_CACHE:
        g = s.get('group')
        if g:
            groups.add(g)
    return sorted(list(groups))
