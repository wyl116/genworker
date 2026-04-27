# genworker

[![CI](https://github.com/wyl116/genworker/actions/workflows/ci.yml/badge.svg)](https://github.com/wyl116/genworker/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Mode](https://img.shields.io/badge/runtime-local--first-2f855a)
![Transport](https://img.shields.io/badge/transport-HTTP%20%2B%20SSE-0ea5e9)

The local-first runtime for digital workers that hold roles, follow rules, and evolve under governance.

[Quick Start](#three-minute-quick-start) | [API](docs/API.md) | [Configuration](docs/CONFIGURATION.md) | [Deployment](docs/DEPLOYMENT.md) | [Architecture](docs/ARCHITECTURE.md)

genworker 是一个面向“数字员工 / 数字工人”场景的 `filesystem-first` runtime。

它不是把一个通用 Agent 包一层 prompt 再长期运行，而是把“岗位”作为系统里的主对象来运行：

- 一个岗位是谁
- 这个岗位负责什么
- 这个岗位能做什么、不能做什么
- 这个岗位和谁协作
- 这个岗位学到的东西如何进入系统

默认形态下，它保留了多 Worker、Skill、Tool、MCP、会话与自治运行时的核心能力，同时把依赖压到最小：

- 默认不要求 Redis
- 默认不要求 MySQL
- 默认不要求 OpenViking
- 默认不启用 IM 渠道
- 默认只依赖本地 `workspace/` 与 `configs/`

## Core Characteristics

- `Role-first, not agent-first`: 主体不是“agent 实例”，而是“岗位 / worker”
- `Organization-aware`: 多个 AI 不是简单 multi-agent 通信，而是有职责边界、协作关系和归属路由
- `Governable by design`: 权限、审计、信任分级、人在回路是系统能力，不靠 prompt 口头约束
- `Learning with approval`: 学习不是自动沉淀自动生效，而是“提议 -> 审核 -> 生效”
- `Goal-driven autonomy`: 主动性不是单纯 cron 到点执行，而是围绕结构化目标做状态偏离判断
- `One runtime, many triggers`: 对话、任务、事件、巡检共享同一条岗位执行管线

## What You Can Do With It

- 为一个组织部署多个数字员工，每个数字员工占一个岗位、各自有职责和边界
- 让同一个岗位同时处理对话、API 任务、事件响应和自主巡检
- 让岗位经验和客户数据分层隔离，避免跨租户或跨客户串数据
- 让 AI 学习新规则，但把生效过程放进“提议 -> 审核 -> 生效”的治理链路
- 让运行时根据目标偏离主动触发动作，而不是只在定时器到点时执行脚本
- 用 HTTP/SSE、workspace、配置模板和调试接口把整条链路先在本地跑通

## How It Is Different

| 维度 | 常见个人助手 / 通用 Agent | genworker |
| --- | --- | --- |
| 主体 | 一个用户的 agent 或 workspace | 一个组织里的岗位 / worker |
| 角色定义 | prompt + 工具配置 | 系统注册的岗位对象 |
| 记忆边界 | 围绕“我”的全局记忆池 | 岗位经验与租户数据分层隔离 |
| 学习方式 | 自动沉淀，往往自动生效 | 提议、审核、生效、衰减的生命周期 |
| 多角色协作 | multi-agent 通信或路由 | 职责边界、协作关系、归属路由 |
| 主动性 | cron / 定时触发 | 目标驱动 / 状态偏离驱动 |
| 工作模式 | 对话、任务、事件常常分散实现 | 对话、任务、事件、巡检共享执行管线 |

## Best Fit

- 一个组织里部署多个数字员工，每个员工占一个岗位、各自有职责边界
- 同一个岗位统一处理对话、任务、事件响应和自主巡检
- 需要把岗位经验与客户数据分层隔离，避免跨租户串数据
- 需要业务规则可审批、操作可审计、关键动作可追溯
- 想先本地把整条运行链路跑通，再逐步接入更复杂基础设施

## Not For

- 给自己用的 AI 伙伴或消息收件箱整合器
- “学到了就自动生效”的无治理自学习 Agent
- 只需要一次性创意任务、不需要长期岗位和组织边界的场景
- 用五套独立系统分别拼对话、任务、事件和巡检的轻量原型

如果你的目标是“给自己用的 AI 伙伴”，Hermes、OpenClaw 一类个人助手轨道通常更合适；如果你的目标是“给组织用、占岗位、可治理、可追溯的数字员工”，`genworker` 更适合。

## Documentation

- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): 配置加载顺序、profile、关键环境变量与路径规则
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): 本地启动、反向代理、systemd 与容器化建议
- [docs/LLM.md](docs/LLM.md): LiteLLM tier、路由模板与参数说明
- [docs/API.md](docs/API.md): 核心 HTTP API、SSE 入口与调试接口
- [docs/RELEASE.md](docs/RELEASE.md): 版本发布、变更校验与维护者发布建议
- [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md): 当前版本新增能力、配置变化与验证结果
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): 运行时架构说明

## What You Get

- HTTP 对话入口：`POST /api/v1/chat/stream`
- Worker 任务流入口：`POST /api/v1/worker/task/stream`
- 健康检查：`GET /health`
- 就绪检查：`GET /readiness`
- 运行时诊断：`GET /api/v1/debug/runtime`
- 本地 Worker / Skill / Persona 加载
- 基于文件系统的会话与工作区运行模式
- 可选 Redis / OpenViking / IM 渠道增强

## Default Operating Model

默认运行方式非常直接：

- 用 `configs/` 管理分层配置
- 用 `workspace/` 管理 tenant、worker、skill、persona
- 用 `python start.py` 直接启动运行时
- 用 `/health`、`/readiness`、`/api/v1/debug/runtime` 观察当前状态

这意味着你可以先在一台普通开发机上跑通，再决定是否引入反向代理、外部存储或更复杂的部署拓扑。

## Three-Minute Quick Start

### 1. Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Prepare Config

最小可运行配置：

```bash
cp configs/config.example.env configs/config_local.env
```

如果你已经习惯只维护 `configs/config_local.env`，也可以直接编辑它；运行时会优先读取 `configs/` 下的分层配置。根目录 `.env.example` 主要用于给容器、CI 或外部启动包装器做参考，不是 `start.py` 的主配置入口。

### 3. Start

```bash
python start.py
```

默认是本地轻量模式：

- `RUNTIME_PROFILE=local`
- `REDIS_ENABLED=false`
- `MYSQL_ENABLED=false`
- `OPENVIKING_ENABLED=false`
- `IM_CHANNEL_ENABLED=false`

### 4. Verify

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/readiness
curl -s http://127.0.0.1:8000/api/v1/debug/runtime
```

如果你看到 `/readiness` 返回成功，并且 `/api/v1/debug/runtime` 里能看到默认 worker 和当前 profile，说明主链路已经启动完成。

## First Requests

对话流示例：

```bash
curl -s -N -X POST "http://127.0.0.1:8000/api/v1/chat/stream" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "你好，帮我概括一下今天应该优先处理什么",
    "thread_id": "chat-001",
    "tenant_id": "demo",
    "worker_id": "analyst-01"
  }'
```

任务流示例：

```bash
curl -s -N -X POST "http://127.0.0.1:8000/api/v1/worker/task/stream" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "检查我的收件箱并整理待办",
    "tenant_id": "demo",
    "worker_id": "analyst-01"
  }'
```

更多接口说明见 [docs/API.md](docs/API.md)。

## Configuration

配置说明见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。

重点规则：

- 配置文件从项目根目录的 `configs/` 读取，不依赖当前 shell 所在目录
- `LOG_DIR` 如果写相对路径，会按项目根目录解析
- 默认 `workspace` 根目录固定为 `<project>/workspace`
- `start.py` 会在启动前切回项目根目录，避免从其他目录启动时路径漂移

可直接参考的模板：

- `.env.example`
- `configs/config.example.env`
- `configs/profiles/local.env`
- `configs/profiles/local_memory.env`
- `configs/profiles/advanced.env`
- `configs/profiles/enterprise.env`

## Core Runtime Model

把 `genworker` 看成四层会更容易理解：

1. Entry Layer: HTTP / SSE / IM / Event / Scheduler
2. Runtime Layer: WorkerRouter、Session、Task、Memory、ToolPipeline
3. Workspace Layer: `workspace/` 中的 tenant、worker、skill、persona 定义
4. Infra Layer: Redis、OpenViking、MySQL、外部平台与代理层

默认本地模式只强依赖前 3 层。

## Runtime Profiles

| Profile | 用途 | Redis | MySQL | OpenViking | IM |
| --- | --- | --- | --- | --- | --- |
| `local` | 最小本地开发与调试 | off | off | off | off |
| `local_memory` | 本地文件系统 + 语义记忆实验 | off | off | on | off |
| `advanced` | 增强型运行时 | on | off | off | off |
| `enterprise` | 完整企业形态模板 | on | on | off | on |

这些 profile 只是模板，不会锁死你的部署方式；最终仍以进程环境变量为最高优先级。

## Repository Layout

```text
.
├── configs/                  # 分层配置与 profile 模板
├── docs/                     # 架构与配置文档
├── frontend/                 # 前端静态资源
├── src/                      # 运行时实现
├── tests/                    # 单元 / 集成测试
├── workspace/                # 默认运行时工作区
├── workspace.example/        # 示例工作区模板
└── start.py                  # 本地启动入口
```

## Development Notes

- 推荐直接使用 `python start.py` 启动
- 如果需要保护接口，可设置 `API_BEARER_TOKEN` 或 `API_KEY`
- `/health` 只看进程是否活着，`/readiness` 看默认主链路是否可服务
- `workspace.example/` 适合用于初始化新的 Worker 目录结构
- `tests/` 同时包含单元测试和集成测试，适合作为二次开发回归基线

架构细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
