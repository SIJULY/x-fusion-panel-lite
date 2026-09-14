import asyncio

from app.core.logging import logger, scheduler
from app.core.state import ADMIN_CONFIG
from app.jobs.domain_ip_sync import job_sync_domain_ips
from app.jobs.geoip import job_check_geo_ip
from app.jobs.monitor import job_monitor_status


DOMAIN_IP_SYNC_INTERVAL_SECONDS = 60


async def startup_sequence():
    scheduler.add_job(job_monitor_status, 'interval', seconds=120, id='status_monitor', replace_existing=True, max_instances=1)
    scheduler.add_job(
        job_sync_domain_ips,
        'interval',
        seconds=DOMAIN_IP_SYNC_INTERVAL_SECONDS,
        id='domain_ip_sync',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info('🕒 APScheduler 定时任务已启动')

    asyncio.create_task(job_check_geo_ip())

    async def init_domain_ip_sync():
        # 启动后先等数据/UI 初始化稳定，再跑一次；之后由 APScheduler 每 1 分钟轮询。
        await asyncio.sleep(30)
        await job_sync_domain_ips()

    asyncio.create_task(init_domain_ip_sync())

    async def init_alert_cache():
        await asyncio.sleep(5)
        if ADMIN_CONFIG.get('tg_bot_token') and ADMIN_CONFIG.get('tg_chat_id'):
            logger.info('🛡️ 正在初始化监控状态缓存...')
            await job_monitor_status()

    asyncio.create_task(init_alert_cache())
