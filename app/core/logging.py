import logging
import sys

import urllib3
from apscheduler.schedulers.asyncio import AsyncIOScheduler


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger('XUI_Manager')
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('nicegui').setLevel(logging.INFO)
# asyncssh 每建立一条连接会打约 10 行 INFO，还会把完整的 base64 远程命令写进日志，
# 单机详情页每刷新一次就是一大段噪音，只保留警告和错误。
logging.getLogger('asyncssh').setLevel(logging.WARNING)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

scheduler = AsyncIOScheduler()
