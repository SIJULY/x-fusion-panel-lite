# X-Fusion Panel Lite

> 本仓库是 [x-fusion-panel](https://github.com/SIJULY/x-fusion-panel) 的**精简版本**，在原项目基础上移除了五块功能：
> 1. 公开监控墙（`/status` 公共状态页，含桌面端与移动端两套渲染）及其配套的三网延迟测速；
> 2. 添加服务器时的 API（X-UI 面板）添加模式，现在只保留 SSH 添加模式；
> 3. 独立的「探针设置」页面，主控端地址与 Telegram 通知已合并进侧边栏的「探针与通知设置」弹窗；
> 4. GitHub 云备份与 OAuth 授权，备份恢复只保留本地 JSON 方式；
> 5. X-UI 面板 HTTP API 引擎（`XUIManager` / `HybridManager`），节点读写统一走 SSH 直连远程数据库。
>
> 其余能力（WebSSH、文件管理、探针、订阅、一键部署、Cloudflare 联动、手机端 SSH 入口等）与原项目一致。安装目录与容器名均带 `-lite` 后缀，可与原版共存。

X-Fusion Panel Lite 是一个面向 **多服务器运维 / VPS 管理 / 节点管理 / 订阅分发 / 探针监控** 场景的可视化管理面板，基于 **NiceGUI + FastAPI + asyncssh** 构建。

它并不只是一个“节点列表页面”，而是把以下几类能力放进了同一套界面中：

- 多服务器聚合管理
- 多模式节点与服务运维
- Root 探针监控与自动注册
- WebSSH + 远程文件管理
- ServerCat 风格手机端 SSH 管理入口
- 原始订阅 / 分组订阅 / 短链订阅
- 一键部署 XHTTP / Hysteria2 / Snell
- 本地 JSON 备份恢复与批量导入

---

## 目录

- [为什么用它](#为什么用它)
- [更新记录](#更新记录)
- [快速开始](#快速开始)
- [核心功能](#核心功能)
- [功能矩阵](#功能矩阵)
- [项目亮点](#项目亮点)
- [界面预览](#界面预览)
- [技术栈](#技术栈)
- [当前目录结构](#当前目录结构)
- [运行方式](#运行方式)
- [默认登录信息](#默认登录信息)
- [主要接口与访问路径](#主要接口与访问路径)
- [运行数据说明](#运行数据说明)
- [Docker--Compose-说明](#docker--compose-说明)
- [安全建议](#安全建议)
- [FAQ](#faq)
- [补充说明](#补充说明)
- [适用场景](#适用场景)

---

## 更新记录

### 2026-08-27
- **Bug 修复**：仪表盘的「📊 服务器流量排行」和「🌏 服务器分布」偶发空白，刷新一下网页又正常。这两张图是 `ui.echart`（canvas），echarts 初始化时会量一次容器宽高；点侧边栏切到仪表盘属于重建 `content_container`，元素挂载那一刻外层 `flex-wrap xl:flex-nowrap` 那行的宽度可能还没算出来，量到 0 就按 0×0 建画布，画出来就是个空白方块——而且 echarts 不会自己再量第二次，所以白了就一直白着。整页刷新时图表属于首屏渲染、布局早已确定，因此刷新就好，这也是它看起来「时好时坏」的原因。同一个坑世界地图那边早就用 `ResizeObserver` 修过（`dashboard.py` 里 `myChart.resize()`），但这两张 `ui.echart` 一直没有同样的保护。现在 3 秒一轮的轮询每次都先 `chart.resize()` 重新量一次容器再画，图表变成自愈的，顺带也能跟着窗口缩放与侧边栏折叠正确重排；另外建完图 60ms 就立刻补拉一次，不用干等满 3 秒。饼图只 `resize()` 不喂接口数据——它的初始数据是 `load_dashboard_stats()` 里按 `group_buckets` 单独算的，和接口 `pie_chart` 的标签格式不一样（接口那份带台数，如 `🇺🇸 美国 (2)`），喂进去会让图例在 3 秒后突然变样。
- **数据恢复**：「数据备份 / 恢复」现在能安全地吃下完整版或另一台面板的备份。原来 `ADMIN_CONFIG.update(data['admin_config'])` 是整份合并，会把精简过程中删掉的功能留下的键（`github_*` 云备份与 OAuth、`probe_custom_groups`、`sync_job_*`、`last_sync_time` 等）一起堆进 `data/admin_config.json`，更要紧的是会把**旧面板的 `manager_base_url` 和 `probe_token` 覆盖到本面板上**——前者是探针上报的目标地址，装探针时会烧进 VPS 上的 `/root/x_fusion_agent.py`，导入旧值会让本面板新装的探针继续报给旧面板。现在按白名单过滤（用白名单而非黑名单：以后不管还删掉什么功能，旧备份里的残留键都自动被挡掉），并把 `manager_base_url`、`probe_token`、`session_version` 三个「本面板身份」键排除在导入之外；恢复完成后如果检测到备份来自另一台面板，会明确提示探针仍上报给旧面板、需要在本面板重装才会切过来。同时给服务器条目补了脏数据保护：`url` 是服务器主键（探针上报、节点缓存、管理器实例全靠它定位），没有 `url` 或不是字典的条目直接跳过并计数，不再往缓存里塞永远连不上的空壳记录。顺带清掉该函数里 4 个赋值后从未使用的局部变量（`new_subs` / `new_config` / `new_ssh_key` / `new_cache`）。
- **文档**：FAQ 新增第 10 条，说明「两台面板管同一批机器」的边界——探针是单个固定名字的 systemd 服务（`x-fusion-agent`），只能上报给一台面板；重装探针只覆盖 agent，不影响 xray / x-ui / hysteria / snell，断的只是原面板的监控数据；部署在软路由等内网设备上时 `manager_base_url` 必须填 VPS 能访问到的地址。

### 2026-08-27
- **安全**：日志不再写入 SSH 凭据。`server` 字典里存着 `ssh_password` 和 `ssh_key`（完整私钥），而 `state.CURRENT_VIEW_STATE['data']` 指向的又是同一个字典——`content_router.py` 与 `sidebar.py` 里 15 处 `f"...data={data}"` / `f"...={CURRENT_VIEW_STATE}"` 会把它整个插进日志，点一台服务器就写好几遍。容器日志既不加密也常被随手贴出来排查问题，等于把私钥摊开放。现在 `core/logging.py` 新增 `scrub()`，按字典形状自动脱敏：server 字典压成 `名称@url`（定位够用），带 `data` 字段的 `CURRENT_VIEW_STATE` 递归处理，分组名和 `None` 原样返回；调用方不用判断传进来的是哪种。另外 `main_page.py` 的 `target_data=` 和 `probe.py` 里「密钥错误」把提交上来的 secret 明文打出来这两处，一并修掉。
- **日志**：27 处 `[ContentRouter]` `[Dashboard]` `[Sidebar]` `[SidebarClick]` `[SaveServerDialog]` `[MainPage]` `[SingleSSHRoute]` 开头的调用链追踪日志从 `logger.info` 降为 `logger.debug`。这些是之前排查「点了没反应 / 视图没刷新」时加的探针，功能验证完就只剩噪音——切一次视图能刷十几行。**需要时把 `app/core/logging.py` 里的 `level=logging.INFO` 改成 `logging.DEBUG` 重启容器即可全部放出来**，该文件里也写了这条注释。真正的运营事件（`🚀 系统正在初始化`、`✅ UI 已就绪`、`♻️ 自动恢复视图`、`🕒 APScheduler 定时任务已启动`、探针自动注册、域名 IP 同步等）保持 INFO 不动；`[Notify]` 只在没有客户端上下文、提示弹不出去时触发，量小且可能是本该让用户看到的报错，也留在 INFO。
- **日志**：6 处面板侧的 `print()` 改成 `logger`。`print` 绕过 logging，既没有时间戳和级别，也不受上面那个开关控制：`services/ssh.py` 每次建连的调试行与 `aggregated_view.py` 的翻页行改为 `logger.debug`，`utils/encoding.py` 的 VLESS 解析失败改为 `logger.warning`，`services/deployment.py` 两处裸 `print(e)` 和 `services/dashboard.py` 的 `print` + `traceback.print_exc()` 改为 `logger.exception`（连带堆栈，比原来只有一行信息量更大）。`services/xui_ssh.py` 与 `single_server.py` 里剩下的 8 处 `print` **不动**——它们在通过 SSH 推到目标 VPS 上执行的脚本模板里，`print` 就是把结果回传给面板的通道。
- **Bug 修复**：切回仪表盘时控制台偶发一条 `ERR_EMPTY_RESPONSE` 红线。轮询现在离开仪表盘会自行停止（见下一条记录），空闲超过 `timeout_keep_alive`（60 秒）后服务端会回收那条 keep-alive 连接，但浏览器并不知道，切回来第一次 `fetch` 复用死 socket 就会零字节断开。请求本身下一拍就自愈，只是控制台会留条红线。现在把拉取逻辑抽成 `window.dashFetchOnce()`，只有网络层异常才返回 `false`，此时立刻重试一次——重试必然走新建的连接，所以第二次一定落在活的 socket 上。
- **精简**：删除移动端页面 `app/ui/pages/mobile_page.py`（391 行）及 `/m`、`/mobile` 两条路由。全项目对它的引用只有 `app/api/auth.py` 里的 1 个 import + 2 行注册，无重定向、无 UA 判断，是一刀干净的切口；它引用的 `WebSSH` / `get_ssh_client` / `check_auth` / `save_admin_config` 在别处仍有调用方，没有留下新的死代码。
- **精简**：`Dockerfile` 移除 `iputils-ping`。三网延迟测速在第一步就随监控墙删掉了，容器内已无任何 ping 调用；一键部署和 X-UI 管理里的 `ping`/`curl` 都在目标 VPS 上通过 SSH 执行，不需要面板镜像自带。`ca-certificates` 保留（httpx 走 HTTPS 调 ip-api.com / Cloudflare / Telegram 时校验证书用），`curl` 保留（应用不用，留着方便进容器排查接口）。
- **精简**：删除 `requests_check.txt`（空文件，且没进 `.gitignore`，是误提交的临时产物）与两个一次性引导脚本 `push-to-lite.sh`、`finish-step3.sh`（本地文件，已在 `.gitignore` 里；前者做的是 `gh repo create` 建仓，仓库早已建好，重跑只会失败，且还在检查已被删除的 `static/x-install.sh`）。
- **说明**：ECharts / xterm.js 仍走 CDN，没有改成本地打包。仓库里 `static/` 共 1.2 MB，其中 `world.json`（仪表盘世界地图，保留）就占 986 KB；把 ECharts 塞进仓库要再加约 1 MB，与精简的方向相反，故不做。
- 结果：Python 文件数 61 → 60，总行数 15,271 → **14,940**。已通过 `python3 -m compileall` 全量语法检查、47 个模块的真实 import 测试（验证无循环导入）、`node --check` 对 9 处内嵌纯字符串 JS 的语法检查、`bash -n install.sh`，以及一次完整启动验证：`/api/dashboard/live_data` 返回 200、`/mobile` 返回 404、`INFO` 与 `DEBUG` 两种级别下的日志量对照，并确认脱敏后的日志里搜不到密码和私钥。

### 2026-08-27
- **Bug 修复**：仪表盘的实时轮询在切走视图后不会停止。`ui/components/dashboard.py` 渲染仪表盘时会往浏览器注入一段 `setInterval(..., 3000)`，每 3 秒请求 `/api/dashboard/live_data`；但全项目唯一的 `clearInterval` 就在这段注入代码自己的第一行，只有**再次渲染仪表盘**时才会执行。于是只要看过一次仪表盘，浏览器就会一直每 3 秒打一次接口，切到区域视图、所有服务器、单机详情页都不停，只有刷新整个页面才断。更糟的是离开仪表盘后这些请求毫无意义——回调里每个 DOM 写入都有存在性判断，而 `#stat-servers`、`#chart-bar` 在其他视图并不存在，服务端白算一遍再把 JSON 丢掉。现在让轮询自检：发现统计卡片的 DOM 不在了就 `clearInterval` 自行停掉，重新进仪表盘时会重新装上。
- **日志**：`services/dashboard.py` 里 `[DashboardCalc] start` / `[DashboardCalc] result=` 两行从 `logger.info` 降为 `logger.debug`。按 3 秒一次计算，这两行原本每天产生约 **57,600 行**日志，且 `result=` 会把完整结果字典写进去（服务器越多这行越长）。
- **日志**：`core/logging.py` 把 `asyncssh` 压到 `WARNING`。它原本继承 root 的 INFO，每建立一条 SSH 连接就打约 10 行，还会把完整的 base64 远程命令（约 2 KB）写进日志——单机详情页每刷新一次就是一大段噪音。
- **运维**：`docker-compose.yml` 与 `install.sh` 生成的 compose 给每个服务补上日志轮转（`max-size: 10m`、`max-file: 3`）。原先没有 `logging:` 配置，docker 默认的 json-file 驱动不限大小，日志文件会一直涨到把磁盘吃满。
- **运维**：`install.sh` 的 `update_source_code` 会在 `git reset --hard` 前后备份并还原 `docker-compose.yml`（保留端口、账号密码与 Caddy 选择），所以已安装的机器拿不到上面这条轮转配置。新增 `ensure_log_rotation`，在还原之后为缺少 `logging:` 的服务补上，已有则跳过；执行 `install.sh` 选 2 更新即可自动生效。

### 2026-08-27
- **精简**：全项目死代码与残留清理（A 类），共移除 **429 行与 6 个文件**，不改变任何功能行为。
- 删除 4 个只剩一行转发 import 的空壳模块：`app/ui/dialogs/ssh_console.py`、`deploy_xhttp.py`、`deploy_snell.py`、`deploy_hysteria.py`（各 3 行）——真正的实现分别在 `services/ssh.py` 与 `services/deployment.py`，全项目零引用。
- 删除 `app/storage/files.py`（25 行，整个文件）：`safe_save` 与 `FILE_LOCK` 的 JSON 落盘链路，在数据层迁到 SQLite 后已零引用。
- 删除 `static/x-install.sh`（8.4 KB）：只被自己的注释提到，安装脚本走的是根目录的 `install.sh`。
- `app/services/server_ops.py` 372 → 165 行：删掉 `silent_refresh_all`（45 行）、`save_server_config`（127 行，与 `server_dialog.py` 里同名函数重复且是旧版）、`get_targets_by_scope`（19 行，`content_router.py` 有自己的实现）。这也顺带断掉了 `server_ops → probe` 与 `server_ops → xui_fetch` 两条导入边。
- `app/core/config.py` 749 → 717 行：删掉 `MATCH_MAP`（29 行，ECharts 地图名映射，随监控墙一起失去调用方）与 `SYNC_COOLDOWN_SECONDS` / `SYNC_COOLDOWN` 两个配置项。
- `app/utils/geo.py` 198 → 141 行：删掉 `get_echarts_region_name`、`ECHARTS_REGION_ALIASES`（监控墙残留）与 `auto_prepend_flag`（智能命名已统一走 `fast_resolve_single_server`）。
- 删除 `generate_converted_link`（`utils/encoding.py`，15 行）：它拼的是给客户端看的 `{域名}/convert?...` 链接，而实际的订阅转换是 `api/subscriptions.py` 在服务端直连 subconverter 容器（`http://subconverter:25500/sub`），界面下发的是 `/get/sub/{target}/{token}` 短链。
- 删除 `format_uptime`（`utils/formatters.py`，8 行）与 `show_custom_node_info`（`ui/components/server_rows.py`，17 行）：都是监控墙时代的展示函数。
- 删除 `state.GLOBAL_UI_VERSION` 与 `state.UI_ROW_REFS`：前者在 `save_servers` / `save_admin_config` 里被写入两次却没有任何读者，后者从未被写入。
- 拆掉 `server_dialog.py` 末尾三个纯转发函数 `render_single_ssh_view` / `render_single_server_view` / `render_aggregated_view`（18 行）：唯一调用方 `content_router.py` 现在直接 import `app.ui.pages.single_ssh` / `single_server` / `aggregated_view`；`cleanup_ssh_route_terminal` 是真实现，保留不动。
- 清理 12 个未使用的模块级导入（`main.py` 的 `FastAPI` / `CORSMiddleware` / `JSONResponse` / `Response` / `StaticFiles`，5 处 `from nicegui import run`，以及 `repositories.py` 里 5 个 JSON 时代的文件路径常量等）。
- **Bug 修复**：侧边栏点击「当前正在看的那台机器」永久无效。`state.REFRESH_CURRENT_NODES` 实际存在三份互不相干的副本——`sidebar.py` 用 `from app.core.state import REFRESH_CURRENT_NODES` 在导入时就把值（那个空 lambda）快照下来了，而 `single_server.py` 写的是自己的模块级同名变量和 `server_dialog` 上一个根本不存在的属性。结果是：写入方和读取方永远碰不到面，这个分支只会调用空 lambda 然后 `return`，节点列表不会刷新。现在收敛成一份：`single_server.py` 写 `state.REFRESH_CURRENT_NODES`，`sidebar.py` 在调用时才从 `state` 上取，重复点击同一台机器可以正常触发重新拉取节点。
- **命名修正**：`show_ping` 改名 `hide_group_column`。三网延迟测速在第一步就随监控墙移除了，这个参数早已与 ping 无关——它真正的作用是把「所在组」列换成「在线状态 / IP」并隐藏状态灯（7 列紧凑布局）。区域视图里每行的所在组都相同，所以只在 `scope == 'COUNTRY'` 时置 True。同步把 `draw_row` 的 `use_special_mode` 参数改成 `compact_mode`，`COLS_NO_PING` / `COLS_SPECIAL_WITH_PING` 改成 `COLS_FULL` / `COLS_COMPACT`，`SINGLE_COLS_NO_PING` 改成 `SINGLE_ROW_COLS`。
- 顺带修掉 `aggregated_view.py` 里一处必然崩溃的兜底分支：`try` 块外的 `except` 没有给 `use_special_mode` 赋值，一旦进入就会在表头渲染时抛 `NameError`；同时删掉两个从未被使用的重复列宽局部变量 `cols_ping` / `cols_no_ping`。
- 结果：Python 文件数 66 → 61，`app/` 总行数 15,700 → **15,271**。已通过 `python3 -m compileall` 全量语法检查，以及全项目跨模块 import 解析与未使用导入两项静态检查。

### 2026-08-27
- **精简**：移除 **X-UI 面板 HTTP API 引擎**，节点读写只保留 SSH 直连远程数据库这一条通道。
- 删除 `app/services/xui_api.py`（223 行，整个文件）：`XUIManager` 的登录、`get_inbounds`、增删改节点等全部 HTTP API 实现。
- `app/services/manager_factory.py` 从 120 行压到 37 行：删掉 `HybridManager`（SSH 优先 + API 兜底的双引擎包装层，四个方法结构完全重复），`get_manager()` 现在直接返回 `SSHXUIManager`；条件不满足时抛出明确异常，由调用方降级展示。
- 删除 `app/jobs/traffic.py`（80 行，整个文件）与启动时的 `traffic_sync` 定时任务：这个 24h「智能同步」轮询会跳过所有带探针的机器，只对没探针的机器走面板 API；API 引擎移除后它已无事可做。随之清掉 `sync_job_index` / `sync_job_start` 两个配置项。
- 删除 `app/utils/async_tools.py`（8 行，整个文件）与 `app/core/logging.py` 中的 `BG_EXECUTOR`：这条「把同步 API 方法丢进线程池」的链路 `run_in_bg_executor` 零调用，只剩两个孤儿导入。现在启动不再多开 20 个空闲线程。
- 顺带修好一处长期失效的分支：单机详情页的 SSH 兜底门卫是 `hasattr(mgr, '_exec_remote_script')`，而 `HybridManager` 并没有这个属性，所以这段兜底一直进不去；换成 `SSHXUIManager` 后它才真正生效，同时删掉专为同步 API 方法准备的 `asyncio.run` 分支。
- 不再写入面板凭据：`auto_register_node` 不再存 `user` / `pass` / `prefix`，`probe_register` 不再塞死 `admin/admin`，单机页也不再自愈 `prefix`（原读者只有已删除的 `XUIManager`）。
- 修掉一处会被 API 移除放大的判定过严问题：原 `HybridManager` 要求 `probe_installed` **且** 填了 `ssh_host` 才建 SSH 引擎，而 `app/services/ssh.py` 本身在 `ssh_host` 为空时会从 `url` 解析主机名。以前这个偏严的门槛无所谓（API 会接住），API 移除后会让探针自注册进来的机器（有探针但没写 `ssh_host`）彻底没有引擎可用。现在统一收敛到 `has_ssh_target()`：要求有探针，且 `ssh_host` / `url` 至少有一个。
- 单机页节点行不再显示「API」标签与蓝色配色（这两个分支已不可能成立），非自定义节点一律标为「Root」。
- **接口变更**：`POST /api/auto_register_node` 的必填参数从 `ip` / `port` / `username` / `password` 收窄为 `ip` / `port`；老调用方继续发送 `username` / `password` 也不会报错，只会被忽略。
- **注意**：这条取代了 2026-08-26 的说明——`data/servers.json` 里老记录的 `user` / `pass` / `prefix` 字段不会被删除，但**已经彻底没有读者**，SSH 不可用时不再有 API 兜底。节点管理现在要求该服务器已安装探针且 SSH 可连。
- **精简**：移除 **GitHub 云备份与 OAuth 授权**，备份恢复只保留本地 JSON 方式。
- 删除 `app/services/github_backup.py`（332 行，整个文件）：仓库/目录管理、OAuth 授权流程、access token 存取、备份上传与下载等 26 个函数。其中 `upload_backup_to_github` 与 `download_latest_backup_from_github` 本就零调用。
- 删除 `app/api/github_oauth.py`（87 行，整个文件）与 `main.py` 中的两条路由 `GET /api/github/oauth/start`、`GET /api/github/oauth/callback`。
- 这条链是独立死链：`github_backup.py` 只被 `api/github_oauth.py` 引用，后者只被 `main.py` 引用，`app/ui/` 零引用——界面上从来没有入口。
- 随之不再写入 `github_access_token` / `github_client_id` / `github_client_secret` / `github_oauth_state` / `github_user_login` 等 10 个配置项。
- 结果：Python 文件数 71 → 66，`app/` 总行数 16,554 → 15,700（两步净减 **854 行与 5 个文件**；删除量为 872 行，另新增 18 行用于 `has_ssh_target()` 与注释）；`main.py` 111 → 100 行，`manager_factory.py` 120 → 37 行。

### 2026-08-27
- **精简**：清理全项目零调用死代码，共移除 **259 行与 3 个文件**（其中定义与逻辑 251 行，另含 7 行空行分隔与 1 行孤儿导入），不改变任何功能行为。
- 删除旧版订阅编辑器 `SubEditor` 与 `open_sub_editor`（`sub_dialogs.py`，196 行）：订阅页实际只走 `AdvancedSubEditor`，旧编辑器早已被架空且零调用；随之清掉因此变成孤儿的 `fetch_inbounds_safe` 导入。
- 删除重复的 `open_create_group_dialog`（`group_dialogs.py`，37 行）：侧边栏新建分组走的是 `open_quick_group_create_dialog`，两者功能重复。
- 删除 `app/ui/components/status_cards.py`（14 行，整个文件）：第一步移除公开监控墙后遗留的空壳，全项目连 import 都没有。
- 删除 `get_ssh_client_sync`（`services/ssh.py`，2 行）：只是 `await get_ssh_client()` 的转发，零调用，且名字里的 `sync` 早已不成立。
- 删除 `app/api/probe.py`（1 行，整个文件）：一行转发 import，没有任何模块引用它，`main.py` 直接从 `app.services.probe` 取函数。
- 删除 `app/core/security.py`（空文件）：模块化迁移期留下的占位文件，从未写入内容。
- 删除 `main.py` 中未使用的 `send_telegram_message` 导入：真正的调用方是 `jobs/monitor.py` 与 `services/traffic_guard.py`，各自已有导入。
- 结果：Python 文件数 74 → 71，`app/` 总行数 16,813 → 16,554；`sub_dialogs.py` 621 → 422 行，`group_dialogs.py` 392 → 353 行，`services/ssh.py` 540 → 535 行。

### 2026-08-26
- **精简**：移除独立的「探针设置」页面，侧边栏不再有该入口。
- 需要保留的两项配置合并进侧边栏底部的「探针与通知设置」弹窗：**主控端外部地址**（Agent 连接地址）与 **Telegram 通知**（Bot Token / Chat ID）。
- 探针总开关仍只在首次运行初始化设置里，弹窗中不再重复提供。
- 原探针页上的「分组管理」「排序视图」一并删除（含 `open_unified_group_manager` / `open_group_sort_dialog` 两个对话框与 `probe_custom_groups` 配置项）；侧边栏分组的日常管理仍在分组右键菜单里。
- 一并移除原探针页的「复制安装命令」「更新所有探针」与数据概览卡片；单台服务器的探针安装/更新仍在服务器弹窗中，批量添加服务器时也会自动推送。
- 清理第一步遗留的节点 TCP 延迟采集链路：`batch_ping_nodes`、`sync_ping_worker`、`PING_CACHE` 与 `PROCESS_POOL`（含启动时的 `ProcessPoolExecutor(max_workers=4)`），这条链在监控墙移除后已无任何调用方，现在启动也不再多开 4 个工作进程。
- 恢复上次视图时若记录的是已删除的探针页，会自动回落到仪表盘。
- **精简**：添加服务器时移除 API（X-UI 面板）添加模式，只保留 SSH 添加模式。
- 服务器弹窗不再有「SSH / 探针」与「X-UI面板」双页签，打开即是 SSH 表单；保存后自动推送探针。
- 批量添加服务器移除 X-UI 端口/账号/密码与「添加 X-UI 面板」开关，输入行 `IP:端口` 现在表示 SSH 端口。
- 删除确认弹窗不再拆分「SSH 连接信息 / X-UI 面板信息」，改为整机删除 + 可选卸载远程探针。
- 说明：`data/servers.json` 中已有的 `user` / `pass` / `prefix` 字段不会被清除，HybridManager 的 API 兜底仍对老数据生效，只是无法再通过界面新增或编辑这些凭据。
- **精简**：移除公开监控墙（`/status` 公共状态页，含桌面端与移动端两套渲染）。
- 同步移除仅为该页面服务的三网延迟测速能力：探针 Agent 的 `ping` 采集、探针页与设置弹窗中的电信/联通/移动目标 IP 配置、Ping 历史趋势缓存。
- 探针安装脚本不再需要 ping 目标参数，也不再安装 `iputils` 依赖。

### 2026-08-22
- **Bug 修复**：修复了 `XUIManager.get_inbounds` 异步调用在部分场景下的兼容性问题，移除了多余的线程池调用。
- **登录优化**：对登录界面的内部流程进行了彻底异步化改造，修复了 IP 归属地查询等操作阻塞主界面的问题。

### 2026-08-21
- **性能优化**：全面重构了后端 HTTP 请求与核心服务，将项目中所有的 `requests` 同步调用替换为基于 `httpx` 的异步非阻塞请求。
- **体验提升**：解决了由于同步网络请求阻塞主线程（Event Loop）导致的网页操作卡顿问题，现在无论是添加节点、探针交互、Cloudflare 同步还是获取订阅，操作都更加丝滑流畅。
- **架构升级**：优化了 `XUIManager`、`CloudflareHandler`、`GithubBackupManager` 以及各项基础服务（订阅、提醒、地理位置）的异步处理逻辑。

### 2026-08-14
- **新增功能**：Cloudflare 配置弹窗新增“导出数据”按钮，可导出 CSV 文件。
- **导出内容**：CSV 包含服务器名称、服务器 IP，以及该 IP 在 Cloudflare 中对应的 A 记录域名解析；同一 IP 对应多个域名时会合并展示。
- **优化修正**：导出时服务器 IP 与“所有服务器”列表一致，会从服务器 URL 主机名解析真实 IP，避免直接导出域名。

### 2026-08-05
- **修复 Bug**：修复了在启用 VPS 流量超限保护功能时，如果初次获取不到探针数据，会导致将基线清零从而将历史流量误算为本周期流量并误触发保护的 Bug。
- **优化体验**：优化了流量超限保护 Telegram 通知文案，将“当前累计流量”修改为“本周期已用”，消除理解歧义。

### 2026-07-27
- **独立节点管理**：支持单独添加通过其他渠道获取的各种代理协议节点，并整合到统一订阅中。

### 2026-07-14
- 新增 Cloudflare 主域名自动绑定机制：VPS 节点链接与明文配置默认使用主域名下发，支持在 CF 记录区自主设为主域名。
- 新增后台自动 IP 同步功能：若在云服务商更换了 IP 并更新了域名记录，系统将自动从 Cloudflare 主域名解析新 IP 并更新 VPS 连接信息，防止失联。

### 2026-07-09
- 优化 X-UI 节点添加逻辑，解决非 root 用户 SSH 或只允许公钥登录时添加失败的问题。
- 引入混合智能降级 (HybridManager)：优先尝试 SSH 模式写入数据库，当 SSH 不可用或权限不足时，无缝回退到 API 模式。
- 支持非 root 用户使用 sudo 操作数据库：只要该用户有 sudo 权限并在节点配置中填写了 SSH 密码，系统会自动完成 sudo 提权。
- 改进 SSH 连接错误提示，提供更友好明确的公钥登录、超时等排查建议。

### 2026-07-02
- 探针公开页面（公开监控墙）增加网格/列表视图切换功能。
- 在列表视图下，VPS 排列改为更紧凑的按行显示。
- 新增列表模式表头，清晰展示状态、系统、节点名称、标签、运行时间、CPU、内存、硬盘、流量和速率等指标。
- 视图选择（网格或列表）会自动保存在用户本地缓存中，刷新页面后保持不变。

### 2026-06-02

- 新增服务器级“流量超限保护”功能，可为单台服务器设置流量阈值（GB）。
- 当 Root 探针上报的累计流量超过阈值时，系统会自动通过 SSH 封禁当前 VPS 的业务端口，阻止继续跑流量。
- 新增流量保护状态持久化字段，支持记录是否已触发、触发时间、已封禁端口和最近一次执行结果。
- 服务器编辑弹窗的 `X-UI 面板` 页签中新增“启用流量超限保护”和“流量阈值 (GB)”配置项。
- 单服务器详情页的“系统信息”卡片中新增流量保护进度、阈值占比、封禁端口和执行结果展示。

### 2026-05-25

- 新增首次运行初始化设置：首次打开面板时可修改管理员用户名和登录密码。
- 新增首次运行探针开关：安装完成后可选择是否启用探针功能。
- 探针关闭时不会自动安装/启用探针，探针页面会显示关闭说明并提供重新启用入口。
- 恢复上次视图时会避开已关闭的探针页面，侧边栏同步显示“探针已关闭”。

---

## 为什么用它

如果你现在的需求不只是“看一下节点列表”，而是希望有一个后台同时处理下面这些事情：

- 管多台 VPS 与多类服务资源
- 快速查看所有服务器在线情况
- 直接改节点、删节点、复制配置
- 远程 SSH、传文件、在线改配置文件
- 给服务器装探针并收集负载 / 在线状态
- 输出订阅、分组订阅、短链订阅
- 一键部署协议并自动写回节点

那这个项目就是为这种场景设计的。

---

## 快速开始

### 1）最快方式：一键安装

```bash README.md
bash <(curl -Ls https://raw.githubusercontent.com/SIJULY/x-fusion-panel-lite/main/install.sh)
```

安装脚本支持：
- IP + 端口直连
- 域名 + Caddy 自动 HTTPS
- 更新保留 `data/`
- 一键卸载

### 2）Docker Compose 启动

```bash README.md
docker compose up -d --build
```

启动后默认访问：
- `http://你的IP:8081`

### 3）源码运行

```bash README.md
python3 -m venv .venv
source .venv/bin/activate
pip install -r app/requirements.txt
python app/main.py
```

### 4）首次进入后建议做的事

建议按这个顺序操作：

1. 登录后台，完成 MFA 绑定
2. 修改默认账号密码和自动注册密钥
3. 保存全局 SSH 私钥
4. 添加第一台服务器
5. 安装 Root 探针
6. 配置 Cloudflare / 订阅 / 分组

---

## 功能矩阵

| 模块 | 当前状态 | 说明 |
|---|---|---|
| 多服务器管理 | ✅ | 支持分组、聚合、单机详情、地区识别 |
| 添加服务器 | ✅ | 仅 SSH 模式（单台添加 / 批量添加），保存后自动推送探针 |
| Root / SSH 管理 | ✅ | 节点读写统一走 SSH 直连远程 X-UI 数据库，要求已安装探针 |
| WebSSH | ✅ | 内置浏览器终端 |
| 手机端 SSH 管理 | ✅ | `/m` / `/mobile` 入口，登录后仅保留 VPS 状态、账号列表、SSH 端口与终端能力 |
| 远程文件管理 | ✅ | 上传、下载、编辑、删除、重命名、chmod |
| 探针系统 | ✅ | 安装、注册、推送、在线检测 |
| 自动注册节点 | ✅ | 支持新服务器接入与后台 SSH 用户探测 |
| 订阅输出 | ✅ | 原始订阅、分组订阅、短链订阅 |
| subconverter 联动 | ✅ | 可对接 Clash / Surge 等目标格式 |
| 一键部署协议 | ✅ | XHTTP-Reality / Hysteria2 / Snell v5 |
| 独立节点管理 | ✅ | 支持单独添加通过其他渠道获取的各种代理协议节点 |
| Cloudflare 联动 | ✅ | 自动解析、域名列表、删除记录 |
| JSON 备份恢复 | ✅ | 导出、恢复、批量导入（本地 JSON，无云端依赖） |
| MFA / 会话安全 | ✅ | TOTP、设备指纹、异地检测、强制下线 |

---

## 一个典型使用流程

### 场景 A：管理你自己的多台 VPS
1. 添加服务器
2. 配置全局 SSH 密钥
3. 安装探针
4. 进入单机页查看节点和流量
5. 通过 WebSSH 或文件管理维护远程服务

### 场景 B：做订阅分发
1. 添加多台服务器和节点
2. 按地区或用途分组
3. 配置订阅项
4. 生成原始订阅 / 分组订阅 / 短链订阅
5. 配合 `subconverter` 输出客户端目标格式

### 场景 C：快速部署新协议
1. 在单机详情页点击部署入口
2. 选择 XHTTP / Hysteria2 / Snell
3. 如有需要配置 Cloudflare
4. 部署完成后自动写回自定义节点
5. 后续继续在单机页中修改或删除

---

## 核心功能


### 1. 多服务器聚合管理
- 统一纳管多台服务器与多类服务资源
- 支持分组、国家/地区聚合、单机详情、聚合视图切换
- 支持根据地区/国家自动命名、国旗标识、地理信息补全
- 支持批量导入服务器、批量 SSH 执行、批量编辑

### 2. 多模式节点运维
- 添加服务器只走 **SSH 模式**（单台添加与批量添加都只需要 SSH 主机、用户、端口和认证方式）
- 节点读写统一走 **SSH 直连远程 X-UI 数据库**（`SSHXUIManager`），要求该服务器已安装探针且 SSH 可连
- 支持非 root 用户 sudo 提权操作数据库，自动探测 `3x-ui` / `x-ui` 的数据库路径
- 支持节点新增、修改、删除、复制链接、复制明文配置
- 支持自定义节点与面板节点混合展示
- X-UI 兼容能力只是其中一部分，而不是项目唯一定位

### 3. 单机详情页
- 集中展示单台服务器的：
  - 在线状态
  - 节点列表
  - 协议 / 端口 / 流量
  - 订阅相关操作
  - 一键部署入口
  - WebSSH / 文件管理入口
- 支持针对单机进行节点编辑、删除、刷新和后续部署操作

### 4. WebSSH + 远程文件管理
- 内置 WebSSH 终端
- 支持目录浏览、文件树、路径跳转
- 支持文本文件在线查看与编辑
- 支持上传、下载、重命名、删除、新建文件/目录
- 支持 chmod 权限修改
- 适合直接进行远程运维，而不必额外打开外部 SSH 工具

### 4.1 ServerCat 风格手机端 SSH 管理入口
- 新增手机端入口：`/m` 与 `/mobile`
- 复用后台已有服务器账号信息、SSH 主机、SSH 端口、用户与认证配置
- 登录后只展示移动端 VPS 状态列表与命令片段管理，不暴露完整电脑端后台功能
- 账号卡片支持查看 `user@host:port`、在线/离线状态、CPU/内存/磁盘/上下行速率、认证类型和 SSH 连通性测试
- 内置手机端 WebSSH 终端，适合在手机浏览器中进行 VPS SSH 管理
- 终端顶部菜单支持粘贴、清屏、退出登录和命令片段选择
- 终端页命令片段选择弹窗仅显示片段名称，点击名称后立即发送对应命令到当前 SSH 会话
- 底部「命令片段」页面支持新增、编辑、删除常用 SSH 命令，并与后台配置持久化共用
- 终端底部提供 `CTRL-C`、`TAB`、`-`、`/`、`ESC`、方向上键等手机常用快捷键
- 状态页支持静默刷新，进入终端或命令片段页面时不会自动打断当前操作

### 5. Root 探针系统
- 支持一键安装/更新探针到服务器
- 支持探针主动注册、状态推送、节点数据同步
- 支持探针离线判定、在线状态实时刷新
- 支持自动识别现有服务器并合并注册结果
- 支持通过自动注册接口把新节点加入面板并后台探测 SSH 用户

### 6. 订阅系统
- 支持原始订阅输出
- 支持按分组聚合订阅
- 支持短链接风格输出
- 支持按目标客户端生成订阅，如 Clash / Surge
- 可配合 `subconverter` 使用转换链路
- 支持订阅编辑，选节点时按服务器分组归类展示
- 支持将独立节点和普通节点混合添加到同一订阅中

### 7. 一键部署能力
当前已内置以下部署能力：
- **XHTTP-Reality**
- **Hysteria 2**
- **Snell v5**

其中：
- XHTTP 部署支持联动 **Cloudflare API** 自动添加解析
- 部署完成后可自动把生成的节点写回当前服务器配置
- 支持对一键部署产生的自定义节点做后续修改、卸载、删除

### 8. Cloudflare 集成
- 支持保存 Cloudflare API Token
- 支持自动读取根域名列表
- 支持部署时自动添加 DNS 解析
- 支持删除对应记录
- 适用于 XHTTP-Reality 等依赖域名的场景

### 9. 数据管理与批量导入
- 支持完整 JSON 备份导出
- 支持从 JSON 恢复服务器、订阅、管理配置、缓存、全局 SSH 密钥
- 支持批量添加服务器
- 支持设置默认 SSH 用户、端口与认证方式
- 支持导入时同步触发 GeoIP 命名、探针安装等后台初始化任务

### 10. 仪表盘与可视化
- 首页提供系统概览卡片
- 支持服务器数量、节点数量、总流量、订阅数量统计
- 支持流量排行柱状图
- 支持地区分布饼图
- 支持全球节点分布地图
- 支持主题切换后的图表同步刷新

### 11. 登录与安全能力
- 后台登录支持 **MFA 二次验证**（首次登录可绑定 TOTP）
- 支持设备指纹写入与 Cookie 识别
- 支持会话版本校验
- 支持异地 / 异常设备登录检测与强制下线
- 支持一键重置全局会话，强制所有设备重新登录

### 12. 部署与运行方式完整
- 支持本地源码运行
- 支持 Dockerfile 构建
- 支持 Docker Compose 启动
- 支持 `install.sh` 一键安装 / 更新 / 卸载
- `install.sh` 支持两种模式：
  - IP + 端口直连
  - 域名 + Caddy 自动 HTTPS

---

## 项目亮点

- **一套面板覆盖运维全链路**：从服务器纳管、节点维护、订阅输出到监控与部署，尽量集中到一个系统中完成
- **以 SSH / Root 运维为主线**：添加服务器只需一套 SSH 凭据，后续节点读写、部署、文件管理都走同一条通道
- **不仅看状态，还能直接操作**：内置 WebSSH、文件编辑、批量 SSH、节点部署与卸载
- **可视化程度高**：首页统计、地图、分布图、手机端 SSH 页面都比较完整
- **探针能力是核心之一**：不是只做被动展示，而是包含安装、注册、推送与缓存链路
- **部署友好**：支持 ARM 环境、Compose、Caddy HTTPS、subconverter 联动
- **代码已模块化**：当前运行代码已迁移到 `app/` 目录，页面、服务、存储、任务分层明确

---

## 界面预览
<img width="1498" height="1446" alt="image" src="https://github.com/user-attachments/assets/9240a298-c546-4ee4-ab84-086c86b36753" />
<img width="1496" height="1443" alt="image" src="https://github.com/user-attachments/assets/68e89751-49c5-4392-b186-44a0dd285468" />
<img width="1498" height="1454" alt="image" src="https://github.com/user-attachments/assets/fb1855e9-faab-4285-af15-4773b0326b03" />
<img width="2996" height="2882" alt="de17c2d3-ddd6-41ce-86c7-a5f11493ffe7" src="https://github.com/user-attachments/assets/d0648a6c-77fd-43e1-96d4-ae1b1a0638d8" />
<img width="1503" height="1441" alt="image" src="https://github.com/user-attachments/assets/29e69174-83c3-48eb-9eec-25d77e313f5d" />
<img width="2982" height="2882" alt="d9f34f42-c9f3-4c69-907f-5e8b04c76afb" src="https://github.com/user-attachments/assets/89fe2b40-2b36-4d7f-ad6a-5f23e33b9f8a" />
<img width="2986" height="2898" alt="aa5e3dfe-5a01-4393-84ae-745fea1d3410" src="https://github.com/user-attachments/assets/b70ca1d8-587f-4460-baca-7993414502d5" />
<img width="2992" height="2886" alt="e0ab05cd-01bf-46b7-9280-224d3b80b596" src="https://github.com/user-attachments/assets/55a41c9b-50cb-4917-ac86-84c62914d5a9" />
<img width="1494" height="1436" alt="image" src="https://github.com/user-attachments/assets/8cd8e0b7-fbbf-4cf8-8d24-ec0b13c3f071" />

---

## 技术栈

- **UI / Web**: NiceGUI, FastAPI
- **SSH / 远程管理**: asyncssh
- **任务调度**: APScheduler
- **认证增强**: pyotp, qrcode
- **图表与地图**: ECharts, world.json
- **部署方式**: Docker, Docker Compose, install.sh, Caddy

---

## 当前目录结构

```text README.md
project/
├─ app/
│  ├─ main.py                            # 正式运行入口
│  ├─ api/                               # 接口与页面路由注册
│  ├─ core/                              # 配置、状态、日志
│  ├─ services/                          # SSH、探针、订阅、部署、Cloudflare 等业务逻辑
│  ├─ jobs/                              # 启动任务、监控任务、流量同步、GeoIP 修正
│  ├─ storage/                           # data/* 读写封装
│  ├─ utils/                             # 编码、格式化、Geo、网络工具
│  └─ ui/
│     ├─ common/                         # 公共通知 / 设置弹窗 / 数据管理弹窗
│     ├─ components/                     # 仪表盘、侧边栏等组件
│     ├─ dialogs/                        # 服务器、节点、订阅、分组、部署、SSH 等弹窗
│     └─ pages/                          # 登录页、主页面、订阅页、单机页
│
├─ static/                               # xterm / echarts / world map 等静态资源
├─ data/                                 # 运行数据目录
├─ Dockerfile
├─ docker-compose.yml
├─ install.sh
└─ app/requirements.txt
```
---

## 运行方式

### 方式一：本地源码运行

```bash README.md
python3 -m venv .venv
source .venv/bin/activate
pip install -r app/requirements.txt
python app/main.py
```

默认监听：
- `0.0.0.0:8080`

### 方式二：Docker Compose

```bash README.md
docker compose up -d --build
```

默认映射：
- 面板：`8081 -> 8080`
- 数据卷：`./data:/app/data`

默认还会启动：
- `subconverter` 服务

### 方式三：install.sh 一键安装 / 更新 / 卸载

仓库地址：

```bash README.md
https://github.com/SIJULY/x-fusion-panel-lite.git
```

直接执行：

```bash README.md
bash <(curl -Ls https://raw.githubusercontent.com/SIJULY/x-fusion-panel-lite/main/install.sh)
```

或手动执行：

```bash README.md
wget -O install.sh https://raw.githubusercontent.com/SIJULY/x-fusion-panel-lite/main/install.sh
chmod +x install.sh
./install.sh
```

脚本支持：
- `1. 新安装面板`
- `2. 更新面板（保留数据）`
- `3. 卸载面板`

安装时可选：
- **IP + 端口直连**
- **域名 + Caddy 自动 HTTPS**

---

## 默认登录信息

默认账号密码读取环境变量；未设置时默认：

- 用户名：`admin`
- 密码：`admin`

相关环境变量：
- `XUI_USERNAME`
- `XUI_PASSWORD`
- `XUI_SECRET_KEY`

> 首次进入后台后会进入 MFA 绑定 / 验证流程。

---

## 主要接口与访问路径

### 页面
- `/`：后台主页面
- `/login`：登录页
- `/m`：手机端 SSH 管理入口，登录后仅显示 VPS 状态列表、SSH 终端和命令片段
- `/mobile`：手机端 SSH 管理入口别名

### 探针 / 自动注册
- `POST /api/probe/register`
- `POST /api/probe/push`
- `POST /api/auto_register_node`：必填 `secret` / `ip` / `port`，可选 `alias` / `ssh_port`（`username` / `password` 已废弃，发送也会被忽略）

### 订阅
- `GET /sub/{token}`：原始订阅
- `GET /sub/group/{group_b64}`：分组原始订阅
- `GET /get/sub/{target}/{token}`：目标客户端短链订阅
- `GET /get/group/{target}/{group_b64}`：目标客户端分组短链订阅

### 仪表盘
- `GET /api/dashboard/live_data`

---

## 运行数据说明

### `data/`
本地开发默认：
- `./data`

Docker 容器默认：
- `/app/data`

常见运行文件：
- `servers.json`
- `subscriptions.json`
- `admin_config.json`
- `nodes_cache.json`
- `global_ssh_key`

### `static/`
主要用于：
- WebSSH 前端终端资源
- ECharts 图表与世界地图
- 仪表盘与状态页渲染所需静态资源

---

## Docker / Compose 说明

当前 `docker-compose.yml` 默认包含两个服务：

- `x-fusion-panel`
- `subconverter`

默认映射：
- `0.0.0.0:8081 -> 8080`

默认数据卷：
- `./data:/app/data`

默认时区：
- `Asia/Shanghai`

---

## 安全建议

- 首次部署后请立即修改默认账号密码
- 请修改自动注册密钥 `XUI_SECRET_KEY`
- 生产环境建议使用 HTTPS 或反向代理
- 不要把真实 `data/` 目录提交到仓库
- 不要提交真实服务器密码、SSH 私钥、TG Token、Cloudflare Token
- 请定期导出备份，至少备份 `data/`
- 对外开放探针接口和自动注册接口时，请务必保护来源和密钥

---

## 补充说明

- 当前仓库已经完成从单文件结构向模块化结构的迁移
- 仓库内包含部分历史迁移辅助文件，用于索引和拆分参考
- 当前主维护方向以 `app/` 目录下模块化代码为准
- 备份恢复只有本地 JSON 一条路径，仓库内不再包含任何云端备份或 OAuth 相关代码

---

## FAQ

### 1. 这个项目适合谁？
适合这几类用户：
- 有多台 VPS，需要统一管理
- 同时使用多套服务或多类节点配置，需要一个聚合后台
- 想把 SSH、文件管理、订阅、部署放到一个界面里
- 想给服务器装探针，在后台集中查看运行状态

### 2. 这个项目依赖某一种面板吗？
不完全依赖。

添加服务器时只需要 SSH 凭据，之后：
- 通过 SSH / Root / 探针模式读写节点与系统信息
- 节点的增删改查都是 SSH 直连远程 X-UI 数据库，不依赖面板的 HTTP 接口是否可达

所以它不是某一个面板的简单皮肤，而是一个更偏“运维控制台”的系统。

### 3. 首次登录后为什么会进入验证码绑定？
因为后台启用了 MFA 流程。首次登录会引导绑定 TOTP，后续登录需要输入动态验证码。

### 4. 探针有什么作用？
探针主要负责：
- 回传在线状态
- 回传节点数据 / 系统信息
- 帮助面板在 Root 模式下更及时地展示服务器状态

### 5. 是否可以不用探针？
单台添加服务器时会默认启用并自动推送探针（SSH 模式下节点数据与系统信息都依赖它）。

如果确实不想装，可以用「批量添加服务器」并取消勾选“启用 Root 探针”，或在侧边栏「探针与通知设置」中关闭探针总开关。

### 6. 是否支持 ARM 机器？
支持。当前项目的 Docker / Compose 方案已经考虑了 ARM 场景，`subconverter` 镜像也做了兼容处理。

### 7. 数据存在哪里？
主要在 `data/` 目录里，包含服务器、订阅、管理配置、缓存、全局 SSH 密钥等。

### 8. 更新会丢数据吗？
正常情况下不会。

- `install.sh` 的“更新面板（保留数据）”会保留 `data/`
- Docker Compose 方式如果保留卷挂载，也不会丢

但仍然建议你在更新前先备份 `data/`。

### 9. 能不能把它当作极简面板来用？
可以，但它的定位本身偏“全功能运维后台”，所以会比极简工具更重、更完整。

### 10. 能直接导入完整版（或另一台面板）的备份吗？两台面板管同一批机器会冲突吗？

**备份可以直接粘贴进来**，「数据备份 / 恢复 → 方式一」会自动做两件事：

- 精简过程中删掉的功能留下的配置项（`github_*` 云备份与 OAuth、`probe_custom_groups`、`sync_job_*` 等）按白名单过滤掉，不会堆进 `data/admin_config.json`
- `manager_base_url`、`probe_token`、`session_version` 这三个键标识「本面板自己」，**始终保留本地值**，不会被旧面板的值覆盖

**但探针只能上报给一台面板。** VPS 上的 agent 是单个固定名字的 systemd 服务（`x-fusion-agent`），面板地址烧死在 `/root/x_fusion_agent.py` 里。所以：

- 只导入备份 → 机器和节点管理都在，但本面板收不到探针推送，服务器状态显示「探针离线 (超时)」
- 在本面板重装一次探针 → agent 被改写成上报给本面板，**原面板从此显示这些机器离线**

**重装探针不会让代理服务断线。** 它只覆盖 `/root/x_fusion_agent.py` 和 `x-fusion-agent.service`，不碰 xray / x-ui / hysteria / snell，节点照常跑。断的只是「原面板的监控数据」。

> 部署在软路由等内网设备上时，`manager_base_url` 必须填 **VPS 能访问到的地址**（DDNS + 端口转发或隧道）。填 `192.168.x.x` 的话 VPS 根本连不上，探针永远上报失败。

---

## 适用场景

如果你需要的是下面这种面板，这个项目会比较合适：

- 同时管理多台 VPS / 多类服务资源
- 既要做节点运维，也要做服务器监控
- 希望把 SSH、文件管理、部署、订阅都集中到一个后台
- 需要支持 ARM 机器、Docker 部署和快捷安装脚本

如果你只需要一个最简单的订阅转换器或最轻量的单一面板前端，那这个项目会偏重一些。
