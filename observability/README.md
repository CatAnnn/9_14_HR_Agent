# 06_emotion SigNoz 监控

## 架构

- SigNoz 使用官方 Foundry 生成和维护独立 Compose 栈。
- SigNoz Web UI 映射到宿主机 7117，内部仍使用 8080。
- signoz_collection_agent 运行在业务 Compose 中，采集：
  - 主机 CPU、内存、磁盘、网络和进程指标。
  - Docker 容器 CPU、内存、磁盘和网络指标。
  - Docker 容器 stdout/stderr 日志。
  - 后端通过 OTLP 上报的 FastAPI、HTTPX、PostgreSQL、Redis traces 和 metrics。
- 采集 Agent 同时加入 Foundry 的 `signoz-network`，直接将数据转发到 `signoz-ingester:4318`，避免宿主机端口回环。

应用 SDK 的日志导出被设为 none，因为容器日志已经由采集 Agent 读取。这样可以避免同一条日志被写入 SigNoz 两次。

## 首次部署

SigNoz Foundry 至少需要 Docker Compose v2 和约 4 GB 可用内存。

    curl -fsSL https://signoz.io/foundry.sh | bash
    ./scripts/signoz.sh deploy
    ./scripts/compose.sh up -d --build signoz_collection_agent local_model_controller backend frontend

打开 http://localhost:7117，首次进入时按 SigNoz 页面创建管理员账号。

## 运维命令

    # SigNoz 状态
    ./scripts/signoz.sh status

    # SigNoz 核心服务日志
    ./scripts/signoz.sh logs

    # 业务系统和采集 Agent 状态
    docker compose ps

    # 采集 Agent 日志
    docker compose logs -f signoz_collection_agent

    # 停止 SigNoz，保留持久化数据
    ./scripts/signoz.sh down

Foundry 生成文件位于 observability/signoz/pours/，该目录不提交到 Git。修改端口或 SigNoz 组件设置时，编辑 observability/signoz/casting.yaml 后重新执行 deploy，不要直接编辑生成的 Compose。

## 数据入口

| 用途 | 地址 |
| --- | --- |
| SigNoz Web UI | http://localhost:7117 |
| SigNoz OTLP gRPC | localhost:4317 |
| SigNoz OTLP HTTP | http://localhost:4318 |
| 业务容器到采集 Agent | http://signoz_collection_agent:4318 |
| 采集 Agent 到 SigNoz ingester | http://signoz-ingester:4318 |

## LLM Token 消耗

后端通过 OTLP 上报累计指标：

    hr_agent.llm.token.usage

该 Counter 每次 LLM 调用分别增加 input 和 output token，并提供以下受控标签：

| 标签 | 含义 |
| --- | --- |
| `gen_ai.token.type` | `input` 或 `output` |
| `gen_ai.request.model` | 实际调用模型 |
| `gen_ai.provider.name` | 模型服务提供方 |
| `hr_agent.task.name` | `employee`、`guidance`、`coach_report` 等任务 |
| `hr_agent.stream` | 是否为流式调用 |
| `hr_agent.request.outcome` | `success` 或 `error` |
| `hr_agent.token.estimated` | token 是否由本地估算 |
| `hr_agent.user.id` | 小写邮箱 `@` 前的账号名；启动预热等后台任务为 `system` |

在 SigNoz 的 Metrics Explorer 中选择 `hr_agent.llm.token.usage`：

- 总消耗：对该指标执行 `sum`。
- 输入/输出拆分：按 `gen_ai.token.type` 分组。
- 各模型消耗：按 `gen_ai.request.model` 分组。
- 各业务任务消耗：按 `hr_agent.task.name` 分组。
- 只看 API 精确值：过滤 `hr_agent.token.estimated = false`。
- 只看成功调用：过滤 `hr_agent.request.outcome = success`。

非流式响应优先使用模型 API 返回的 usage；流式接口当前未返回 usage 时使用本地估算，并通过 `hr_agent.token.estimated=true` 明确标记。OTel 默认每 30 秒导出一次，因此新数据可能延迟约一个导出周期显示。

## LLM 费用

费用使用 Model Farm 人民币单价。运行配置和完整模型价格位于 `backend/config/.env`，可提交的参考配置位于 `backend/config/.env.example`：

```dotenv
LLM_PRICING_VERSION=model-farm-cny-2026-07-21
LLM_MODEL_PRICING='{"glm-5.2":{"input_per_million":8.0,"cached_input_per_million":2.0,"output_per_million":28.0},"qwen3.7-plus":{"input_per_million":6.0,"output_per_million":24.0,"tiers":[{"max_input_tokens":256000,"input_per_million":2.0,"output_per_million":8.0}]}}'
```

- `input_per_million`、`cached_input_per_million`、`output_per_million` 的单位均为 CNY / 1,000,000 tokens。
- `tiers` 按本次请求的输入 Token 数选择；未超过 `max_input_tokens` 时使用该档，超过所有阈值后使用模型默认价格。
- 键优先使用 `provider/model`，找不到时回退到纯 `model`，便于同一模型在不同网关使用不同价格。
- 修改价格时同步更新 `LLM_PRICING_VERSION`，这样历史时序可以区分计价版本。
- 未配置价格的调用不会伪造为零费用，而是累加 `hr_agent.llm.pricing.missing`。
- `hr_agent.llm.cost` 按 input/cached_input/output 分开累计，并以 `hr_agent.cost.estimated` 区分 API usage 与本地 Token 估算。

新增指标：

| 指标 | 含义 |
| --- | --- |
| `hr_agent.llm.request` | LLM API 调用次数，包含成功和失败请求 |
| `hr_agent.llm.request.duration` | 单次 LLM 调用端到端耗时 |
| `hr_agent.llm.cost` | 按配置单价计算的人民币费用 |
| `hr_agent.llm.pricing.missing` | 没有匹配价格的调用次数 |

这里计算的是模型 API Token 费用，不包含自建 SigNoz、PostgreSQL、本地 GPU、存储和电力成本。

## 统一看板

成本与质量看板：

```text
observability/signoz/dashboards/hr-agent-operations-api-cost-otlp-v1.json
```

多用户与总体系统看板：

```text
observability/signoz/dashboards/hr-agent-users-system-otlp-v1.json
```

在 SigNoz 打开 `Dashboards`，选择 `+ New dashboard` → `Import JSON`，分别上传需要的文件。成本看板继续采用两个紧凑区域：

- `LLM API 成本与用量`：人民币 Token 计费总额、调用数、Token 总量、未定价调用，以及按模型汇总的费用和 input/output Token 趋势。
- `调用质量与服务健康`：按模型汇总的 LLM P95、整体结构化输出降级率、整体后端 API P95 和错误率。

看板不再展示容器 CPU/内存、Guidance 内部流式里程碑、按任务或路由展开的大量曲线。这些数据仍保留在 SigNoz 中，需要排障时可从 Metrics Explorer 查询。

多用户与总体系统看板与成本看板分离，避免把日常费用页面重新堆满。它只保留：

- `多用户使用`：当前看板时间范围内的去重用户数、具体账号、各账号总费用和总 Token。
- `总体系统`：后端请求速率、P95、错误率，以及按全部 CPU/GPU 总容量归一化的使用率。

后端新增以下指标：

| 指标 | 含义 |
| --- | --- |
| `hr_agent.user.active` | 当前进程最近 5/15 分钟的诊断用活跃用户数，不用于当前范围看板 |
| `hr_agent.user.request` | 已认证 HTTP/WebSocket 请求数 |
| `hr_agent.user.request.duration` | 已认证请求耗时 |
| `hr_agent.rehearsal.turn` | 多轮预演完成轮数，按当前账号、传输方式和结果聚合 |
| `hr_agent.rehearsal.turn.duration` | 单轮预演各操作及用户可见里程碑耗时（毫秒） |
| `hr_agent.system.gpu.utilization` | 每张可见 NVIDIA GPU 的利用率（0～1） |
| `system.cpu.logical.count` | 宿主机逻辑核心数，用于把容器 CPU 总量归一化到 0～100% |

用户维度取规范化邮箱中 `@` 前的账号名，例如 `user.name@bosch.com` 只上报 `user.name`，不导出完整邮箱、域名、姓名、会话 ID 或内部用户 ID。不同域名存在相同账号名时会合并为同一监控用户。看板通过 ClickHouse 在所选起止时间内查询 `hr_agent.user.request` 的实际样本并按账号去重，因此多个后端副本不会重复计数。

预演耗时可在 Metrics Explorer 中按 `hr_agent.user.id` 过滤当前账号，再以 `hr_agent.rehearsal.operation` 区分会话锁、会话读取、动机评分、情绪反应、知识检索、员工回复、语音合成、保存及总耗时。并行分析阶段保留独立时间区间，不能将各子操作直接相加作为整轮耗时。模型接口目前不提供可信的隐藏思考边界，因此系统只记录可验证的 `generation_to_first_visible_ms` 与 `wait_for_first_visible_reply_ms`，不虚构思考耗时。`total_ms` 的边界是预演服务入口至业务处理完成，最终 timing 元数据的观测性写回不计入其中；浏览器侧另以 `submit_to_done_ms` 覆盖认证、网络和页面处理在内的用户实际等待时间。

Docker Stats 会把 Compose project/service 标签提升为指标属性，因此 CPU 面板可自动汇总当前和以后扩容的全部后端及本地模型副本，再将单核百分比总和除以宿主机逻辑核心数；逻辑核心数取最大值，避免采集器重建后的历史序列重复累加。GPU 面板对所有可见 GPU 的 0～1 利用率取平均再乘以 100；无论 CPU 核心或 GPU 数量多少，整体总容量均为 100%。

所有查询均不固定 `stepInterval`，由 SigNoz 根据当前时间范围自动选择聚合间隔，避免长时间范围下的最小间隔警告。

建议至少配置三条告警：`hr_agent.llm.pricing.missing > 0`、结构化输出 fallback 率超过 2%、后端 API 错误率超过 1%。部署新指标后需重建或重启 backend，并等待一个 OTel 导出周期再检查面板。

## 安全说明

- 业务后端没有挂载 Docker socket。
- 采集 Agent 只读挂载 Docker socket 和容器日志目录。
- local_model_controller 保留原有 Docker socket 写权限，只用于启停白名单内的本地 Qwen 服务。
- Foundry 默认把 4317、4318 和本项目配置的 7117 暴露到宿主机。部署到非受信网络时，应使用主机防火墙或反向代理限制访问。
