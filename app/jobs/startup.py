import asyncio

from app.core.logging import logger, scheduler
from app.core.state import ADMIN_CONFIG
from app.jobs.geoip import job_check_geo_ip
from app.jobs.monitor import job_monitor_status


async def startup_sequence():
    # 域名 IP 同步以前也在这里：每小时全量 + 启动时一次，是启动后那串刷屏
    # Cloudflare 日志的来源。现在改成按需——只在打开单机详情页时同步那一台，
    # 见 app/services/domain_sync.py。
    scheduler.add_job(job_monitor_status, 'interval', seconds=120, id='status_monitor', replace_existing=True, max_instances=1)
    scheduler.start()
    logger.info('🕒 APScheduler 定时任务已启动')

    asyncio.create_task(job_check_geo_ip())

    async def init_alert_cache():
        await asyncio.sleep(5)
        if ADMIN_CONFIG.get('tg_bot_token'):
            logger.info('🛡️ 正在初始化监控状态缓存...')
            await job_monitor_status()

    asyncio.create_task(init_alert_cache())
