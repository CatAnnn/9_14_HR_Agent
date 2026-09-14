# 组织数据爬虫（Linux 服务器）

该目录可以直接在当前 Linux 服务器运行。启动器会优先使用项目虚拟环境，其次使用当前服务器已经安装好依赖的 `hr_agent` Conda 环境。无图形桌面时默认使用 headless 模式；当前服务器选择 Microsoft Edge，只有未安装 Edge 时才选择 Google Chrome。

现有登录资料保存在 `.playwright-profile/`，结果保存在 `results/`。默认会复用登录资料与断点；`SPIDER_RESUME=0` 会重建爬取结果，导出命令会更新 Excel 文件。

## 快速开始

```bash
cd /home/uay4sgh/hr_agent/06_emotion/spider

# 检查 Python、依赖、目录权限、浏览器和显示环境
./run_linux.sh check

# 用临时 profile 启动 about:blank，验证浏览器运行环境；不访问业务网站
./run_linux.sh browser-check

# 只爬取，支持从现有 checkpoint 继续
./run_linux.sh crawl

# 只从已有 pages.jsonl 导出 Excel
./run_linux.sh export

# 依次爬取并导出
./run_linux.sh all
```

导出结果按“最终页面 URL + 内容语言”全局去重。同一页面从多个组织导航被发现时，Excel 只保留一行，并在 `Discovered In Organizations` 中保留全部来源；`Ownership Confidence` 用于区分 URL 明确归属、较短导航路径归属和仍有歧义的共享页面。仅严格识别的 `cmcall=true&perm_link=...` 推荐链接会与无查询参数的同页合并，其他业务查询参数和不同语言页面不会被误合并。URL 和语言仅用于内部去重，不写入 Excel。

Excel 固定导出 `Organization`、`Discovered In Organizations`、`Ownership Confidence`、3 个 `Department Level`、`Page Topic`、`Title`、可选的 `Department Path` 和 `Content`。不会导出 `Page Owner Department`、`Last Changed`、`Language`、`URL`、`Sidebar Complete`。

导出时仍会从正文尾部移除完整匹配的英/德文 WCMS 编辑工具栏，并在内部保留其中的更新时间和页面负责部门。没有实际正文的页面不会进入 Excel；侧边栏不完整但正文有效的页面仍会保留，侧栏质量统计、owner 推断、URL/语言去重逻辑保持启用。原始 `pages.jsonl` 不会被修改。

可从任意工作目录调用 `run_linux.sh`；脚本会自动定位自身目录。请不要直接运行 `spider11.py`，这样可以确保使用正确的 Python、登录 profile 锁、输出锁和一致的环境配置。

一个登录 profile 只能固定给一种浏览器使用。爬取和认证不会把 Edge profile 自动交给 Chrome 或其他 Chromium 版本打开；如果确实要切换浏览器，必须同时设置一个全新的 `SPIDER_PROFILE_DIR` 并重新登录。

## 首次登录或会话过期

Headless 运行会复用 `.playwright-profile/` 中已有的登录会话。如果会话过期，爬虫会停止并给出明确提示，不会在隐藏的 Xvfb 窗口里等待人工登录。

请通过 SSH X11 转发或服务器远程桌面建立可见图形会话，确认 `DISPLAY` 或 `WAYLAND_DISPLAY` 已设置后运行：

```bash
./run_linux.sh auth
```

在打开的浏览器中完成 SSO 登录，页面进入组织视图后，认证命令会自动保存 profile 并退出，不需要回到终端按 Enter。完成后即可重新执行 headless 爬取。

## 环境变量

所有变量均可在命令前临时设置，也可写入 systemd 的 `EnvironmentFile`。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `SPIDER_PYTHON` | Python 解释器绝对路径 | 自动探测 |
| `SPIDER_HEADLESS` | `1/0`、`true/false` | Linux 无显示环境时为 `1` |
| `SPIDER_BROWSER_CHANNEL` | `msedge`、`chrome`；设为空字符串时使用 Playwright Chromium | Edge 优先，否则 Chrome |
| `SPIDER_BROWSER_EXECUTABLE` | 自定义浏览器可执行文件绝对路径 | 空 |
| `SPIDER_BROWSER_ARGS` | 额外浏览器参数，按 shell 规则拆分 | 空 |
| `SPIDER_IGNORE_HTTPS_ERRORS` | 是否忽略 TLS 证书错误；仅在企业 CA 尚未安装时临时使用 | `0` |
| `SPIDER_STATE_DIR` | profile 和默认结果目录的基准目录 | 当前 `spider/` |
| `SPIDER_OUTPUT_DIR` | 结果目录 | `spider/results/` |
| `SPIDER_PROFILE_DIR` | 登录 profile 目录 | `spider/.playwright-profile/` |
| `SPIDER_WORKER_PROFILE` | `auto`、`balanced`、`fast`、`safe` | headless 为 `balanced` |
| `SPIDER_CONCURRENCY` | 网络请求并发数 | 由 worker profile 决定 |
| `SPIDER_PARSE_WORKERS` | HTML 解析线程数 | 由 worker profile 决定 |
| `SPIDER_RENDER_CONCURRENCY` | 浏览器渲染并发数 | 由 worker profile 决定 |
| `SPIDER_REQUESTS_PER_SECOND` | 全局请求速率 | 由代码默认值决定 |
| `SPIDER_MAX_PAGES` | 最大页面数；`0` 表示不限 | `0` |
| `SPIDER_MAX_DEPTH` | 最大深度；`0` 表示不限 | `0` |
| `SPIDER_RESUME` | 是否读取现有 checkpoint | `1` |
| `SPIDER_FAIL_ON_PAGE_ERRORS` | 有待重试页面时是否返回非零状态 | 普通命令为 `0`，服务模板为 `1` |

示例：

```bash
SPIDER_WORKER_PROFILE=safe SPIDER_REQUESTS_PER_SECOND=2 ./run_linux.sh crawl
SPIDER_OUTPUT_DIR=/data/spider-results ./run_linux.sh export
```

## 作为 systemd 用户服务运行

仓库提供了用户级服务模板 `systemd/bosch-org-spider.service`。它不会以 root 身份运行，也不会接触其他用户目录。

```bash
mkdir -p ~/.config/systemd/user
cp systemd/bosch-org-spider.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bosch-org-spider.service
```

当前服务器的用户 linger 默认未开启。若需要在注销 SSH 后继续运行或随服务器启动，请由管理员执行一次：

```bash
sudo loginctl enable-linger uay4sgh
```

查看状态与日志：

```bash
systemctl --user status bosch-org-spider.service
journalctl --user -u bosch-org-spider.service -f
```

如果服务因网络或 SSO 连续失败进入 start-limit，完成登录或恢复网络后执行：

```bash
systemctl --user reset-failed bosch-org-spider.service
systemctl --user restart bosch-org-spider.service
```

如需自定义目录、浏览器或并发，请复制模板后修改 `Environment=`，或添加 `EnvironmentFile=/绝对路径/spider.env`。定时运行时建议改用 systemd timer，避免服务成功结束后无意义重启。

## 安装独立环境（可选）

当前服务器的 `hr_agent` 环境已包含所需依赖。若希望完全独立：

```bash
cd /home/uay4sgh/hr_agent/06_emotion/spider
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

系统 Edge 或 Chrome 已安装时无需下载 Playwright Chromium。若必须使用 Playwright 自带浏览器，再执行：

```bash
./.venv/bin/python -m playwright install chromium
```

## 验证

```bash
./run_linux.sh check
conda run -n hr_agent python -m unittest discover -s tests -v
```

测试均为离线测试，不访问内部网站，也不修改真实 profile 与结果目录。
