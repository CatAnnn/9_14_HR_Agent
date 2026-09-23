# 领导力绩效反馈与对话预演 Agent

本项目是面向管理者的绩效反馈与对话预演应用。用户可以整理员工资料和反馈意图，生成沟通建议，与模拟员工进行文字或语音预演，并查看辅导报告。仓库同时包含知识库处理、模型服务、管理端、可观测性和评测相关代码。

## 主要功能

- **对话准备**：整理员工画像、目标、绩效表现和管理者的沟通意图。
- **沟通辅导**：结合业务配置与知识检索生成反馈建议，并提供相关资料引用。
- **情景预演**：与模拟员工对话，支持语音识别、语音播放和过程中的状态更新。
- **复盘报告**：根据预演过程生成辅导报告。
- **管理与资源**：提供管理端、资料阅读和知识库维护能力。

前端工作流入口见 `frontend/src/App.tsx`，后端 API 入口见 `backend/main.py`。具体能力受配置的模型、语音服务和数据资源影响。

## 技术组成

| 部分 | 主要实现 |
| --- | --- |
| 前端 | React、TypeScript、Vite；生产模式通过 Nginx 提供页面与 API 代理 |
| 后端 | Python 3.11、FastAPI、LangChain、LangGraph |
| 数据与检索 | PostgreSQL、pgvector、Redis、知识库同步与检索服务 |
| 模型与语音 | 可切换本地或平台 Embedding/Reranker；聊天 API、ASR、Fish TTS 按配置启用 |
| 部署 | Docker Compose 组合文件与 `scripts/compose.sh` 包装脚本 |

## 部署前准备

1. 准备 Docker、Docker Compose 和 Bash。`scripts/compose.sh` 使用 Bash 与 Linux 容器相关命令；在 Windows 上应从适用的 Linux/WSL 环境执行。
2. 准备可用的聊天模型接口与凭据。示例配置中的 `API_KEY` 为空，需要按实际环境填写。
3. 按所选模式准备模型资源。示例配置默认 `MODEL_PROVIDER_MODE=local`、`ASR_PROVIDER_MODE=local`、`TTS_ENABLED=true`，相关服务需要对应的 GPU、模型文件及镜像构建条件；也可按实际接入方式调整配置。
4. 准备前端 TLS 证书卷。Compose 将外部卷 `docker_proxy_certs`（可通过 `FRONTEND_TLS_CERT_VOLUME` 调整）挂载到 `/etc/nginx/certs`，其中应有 `server.crt` 和 `server.key`。
5. 检查 `deployment/compose/compose.services.yml` 中镜像构建及模型下载使用的代理、证书挂载和网络地址，确认它们适用于部署主机。

## 配置与启动

在项目根目录执行：

```bash
cp backend/config/.env.example backend/config/.env
```

编辑 `backend/config/.env`。至少检查以下配置：

| 配置项 | 用途 |
| --- | --- |
| `API_KEY`、`CHAT_API_ENDPOINT` | 聊天模型鉴权与地址 |
| `MODEL_PROVIDER_MODE` | `local` 或 `platform`，决定 Embedding/Reranker 服务来源 |
| `ASR_PROVIDER_MODE` | `local`、`bosch` 或 `browser` |
| `TTS_ENABLED` | 是否启动语音合成服务 |
| `APP_RUNTIME_MODE` | `production` 或 `development`；开发模式启用前后端源码热更新 |
| `FRONTEND_HTTPS_HOST`、`FRONTEND_HTTPS_PORT` | 前端 HTTPS 主机名与端口；未设置时使用 Compose 中的默认值 |

配置文件包含更多数据库、GPU、模型、鉴权及知识库选项。它已被 `.gitignore` 排除，请勿提交真实密钥。

```bash
# 检查最终 Compose 配置
./scripts/compose.sh config --quiet

# 构建并启动当前配置对应的服务
./scripts/compose.sh up -d --build

# 查看状态和日志
./scripts/compose.sh ps
./scripts/compose.sh logs -f backend frontend
```

前端默认监听 HTTPS 端口 `7443`，HTTP 端口 `7110` 会重定向到 HTTPS；实际访问地址取决于 `FRONTEND_HTTPS_HOST`、端口、证书和部署主机。后端健康检查经前端代理访问 `/api/v1/health`。首次启动可能包含镜像构建、模型准备和知识库同步，完成时间取决于环境与数据量。

`docker-compose.yml` 会按 `.env` 中的模式包含 `deployment/compose/` 下的组合文件。日常操作建议使用 `scripts/compose.sh`，该脚本固定项目范围并处理后端副本的运行状态。修改运行模式后重新执行 `up`。

## 开发与检查

前端开发命令：

```bash
cd frontend
npm ci
npm run dev
npm run build
npm test
```

后端依赖与测试命令（从项目根目录执行）：

```bash
python -m pip install -r backend/requirements.txt
python -m pytest tests
```

前端 `npm run dev` 仅启动 Vite 页面服务；完整应用仍依赖后端和相关服务。也可设置 `APP_RUNTIME_MODE=development` 后通过 Compose 启动，使用统一的 HTTPS 入口进行开发。部分测试与脚本需要外部服务、模型或专用环境，应按其测试说明和配置运行。

## 目录说明

| 路径 | 内容 |
| --- | --- |
| `backend/` | API、Agent、工作流、服务、配置与检索代码 |
| `frontend/` | 页面、组件、前端 API 客户端与静态资源 |
| `deployment/compose/` | 按模型、语音和运行模式拆分的 Compose 配置 |
| `data/`、`Ebook/` | 知识资料、前端资源及电子书 |
| `scripts/` | 部署包装、知识库同步、数据导入和基准测试脚本 |
| `tests/`、`frontend/tests/` | 后端与前端测试 |
| `eval/`、`loadtests/` | 评测资料及负载测试 |
| `docs/`、`observability/` | 专题文档与可观测性配置 |

知识库同步入口为 `scripts/sync_kb.sh`；更多部署和专项目录说明请查阅相应目录中的文档与脚本。
