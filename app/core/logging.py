import logging
import sys

import urllib3
from apscheduler.schedulers.asyncio import AsyncIOScheduler


logging.basicConfig(
    # ↓↓↓ 想看详细追踪日志就把这里改成 logging.DEBUG ↓↓↓
    # 面板里各处 [ContentRouter] [Dashboard] [Sidebar] [SaveServerDialog] [MainPage]
    # 开头的日志都是 logger.debug，平时不打印。排查「点了没反应 / 视图没刷新 / 数据不对」
    # 这类问题时把下面这行换成 logging.DEBUG，重启容器就能看到完整调用链。
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
# tzlocal 在 DEBUG 级别会打 /etc/localtime 的探测过程，纯噪音
logging.getLogger('tzlocal').setLevel(logging.WARNING)
# httpx 每发一个请求都在 INFO 打一行 `HTTP Request: GET ... "200 OK"`。Cloudflare
# 那套调用一次能刷十几行，把真正要看的日志顶走，而请求成功与否调用方本来就会自己报。
logging.getLogger('httpx').setLevel(logging.WARNING)


def scrub(value):
    """
    日志脱敏。

    server dict 里存着 ssh_password 和 ssh_key（完整私钥），而
    CURRENT_VIEW_STATE['data'] 指向的又是同一个 dict——任何
    f"...{server}" / f"...{CURRENT_VIEW_STATE}" 都会把凭据明文写进
    docker logs，而容器日志既不加密也常被随手贴出来排查问题。

    按 dict 的形状自动判断，调用方不用关心传进来的是哪种：
      - server dict（带 ssh_password / ssh_key）→ "名称@url"，定位够用
      - 带 data 字段的 dict（CURRENT_VIEW_STATE）→ 递归处理 data
      - 其它值（分组名、None、字符串）→ 原样返回
    """
    if isinstance(value, dict):
        if 'ssh_password' in value or 'ssh_key' in value:
            return f"{value.get('name')}@{value.get('url')}"
        if 'data' in value:
            return {**value, 'data': scrub(value['data'])}
    return value

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

scheduler = AsyncIOScheduler()
