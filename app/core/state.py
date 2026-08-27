import asyncio


SERVERS_CACHE = []
SUBS_CACHE = []
NODES_DATA = {}
ADMIN_CONFIG = {}
INDEPENDENT_NODES_CACHE = []

SYNC_SEMAPHORE = asyncio.Semaphore(50)

IP_GEO_CACHE = {}
PROBE_DATA_CACHE = {}
DNS_CACHE = {}
DNS_WAITING_LABELS = {}
ALERT_CACHE = {}
FAILURE_COUNTS = {}

# 订阅拉取统计 {token: {count, last_at, last_ua, last_ip}}。
# 只放内存、不落库：/sub/{token} 是公网无鉴权端点，每次拉取写一次 DB 等于留了个
# 放大写入的口子。重启清零可以接受，UI 上已注明。
SUB_ACCESS_STATS = {}

DASHBOARD_REFS = {}
SIDEBAR_UI_REFS = {
    'groups': {},
    'rows': {},
}

EXPANDED_GROUPS = set()
REFRESH_LOCKS = set()
LAST_SYNC_MAP = {}
REFRESH_CURRENT_NODES = lambda: None
CURRENT_VIEW_STATE = {'scope': 'DASHBOARD', 'data': None}
