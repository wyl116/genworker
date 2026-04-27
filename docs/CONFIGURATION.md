# genworker Configuration Guide

## 1. Overview

`genworker` 使用分层配置：

1. `configs/config.env` 和 `configs/config_local.env`
2. 进程环境变量
3. LiteLLM 专用配置

基础运行参数和 LiteLLM 路由配置分离管理。

## 2. Runtime Config Layers

### 2.1 `configs/config.env`

仓库内的公共默认配置。

### 2.2 `configs/config_local.env`

本地私有覆盖，不提交到 git。

### 2.3 Process Environment

部署系统中的最终覆盖层。

## 3. Profile Templates

### 3.1 `configs/config.example.env`

用于生成 `configs/config_local.env` 的起点。

### 3.2 `configs/profiles/*.env`

这些文件描述不同运行档位的默认能力边界：

- `local.env`
- `local_memory.env`
- `advanced.env`
- `enterprise.env`

## 4. LiteLLM

### 4.1 Local Template

公开导出的默认 LiteLLM 模板是：

- `configs/litellm_local.json.example`

本地运行时复制为：

```bash
cp configs/litellm_local.json.example configs/litellm_local.json
```

`configs/litellm_local.json` 负责定义：

- `default_tier`
- `model_list`
- `tier_aliases`
- `fallbacks`

### 4.2 Tier Contract

配置层只暴露四个 base tier：

- `fast`
- `standard`
- `strong`
- `reasoning`

带工具的调用不会再出现在 alias key 中。`requires_tools` 由 routing policy 内部处理，
其中 `fast + tools` 会自动升级到 `standard`。

### 4.3 Non-Local Injection

`test` / `production` 环境不再读取仓库里的 `litellm_*.json` 文件。
启动前必须通过以下方式之一注入：

- `LITELLM_CONFIG_SOURCE=json` + `LITELLM_CONFIG_JSON`
- `LITELLM_CONFIG_SOURCE=file` + `LITELLM_CONFIG_PATH`

`nacos` 仅保留接口，不在本版本实现。

## 5. Recommended Startup Patterns

### 5.1 Minimal Local Run

```bash
cp configs/config.example.env configs/config_local.env
cp configs/litellm_local.json.example configs/litellm_local.json
python start.py
```

### 5.2 Local Run With Custom Logs

```bash
LOG_DIR=/tmp/genworker-logs python start.py
```

### 5.3 Non-Local Run With Injected LiteLLM JSON

```bash
ENVIRONMENT=production \
LITELLM_CONFIG_SOURCE=file \
LITELLM_CONFIG_PATH=/etc/genworker/litellm.json \
python start.py
```

## 6. Key Environment Variables

### 6.1 Core Runtime

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | 环境名 |
| `RUNTIME_PROFILE` | `local` | 运行时 profile 名称 |
| `COMMUNITY_SMOKE_PROFILE` | `false` | 轻量 smoke profile |
| `SERVICE_NAME` | `genworker` | 服务名 |

### 6.2 HTTP

| Variable | Default | Meaning |
| --- | --- | --- |
| `HTTP_HOST` | `0.0.0.0` | 绑定地址 |
| `HTTP_PORT` | `8000` | 监听端口 |
| `HTTP_WORKERS` | `1` | Uvicorn worker 数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

### 6.3 LiteLLM Local Variables

最小必填参数包括：

- `LITELLM_FAST_MODEL`
- `LITELLM_FAST_API_BASE`
- `LITELLM_FAST_API_KEY`
- `LITELLM_STANDARD_MODEL`
- `LITELLM_STANDARD_API_BASE`
- `LITELLM_STANDARD_API_KEY`
- `LITELLM_STRONG_MODEL`
- `LITELLM_STRONG_API_BASE`
- `LITELLM_STRONG_API_KEY`
- `LITELLM_REASONING_MODEL`
- `LITELLM_REASONING_API_BASE`
- `LITELLM_REASONING_API_KEY`

### 6.4 LiteLLM Injection Variables

- `LITELLM_CONFIG_SOURCE`
- `LITELLM_CONFIG_JSON`
- `LITELLM_CONFIG_PATH`

## 7. Troubleshooting

### 7.1 Why Does Non-Local Startup Exit Immediately?

优先检查：

- `ENVIRONMENT` 是否设为非本地值
- `LITELLM_CONFIG_SOURCE` 是否提供
- 注入的 LiteLLM JSON 是否完整

### 7.2 Why Is LLM Still Unavailable Locally?

优先检查：

- `configs/litellm_local.json` 是否存在
- 12 个 `LITELLM_{TIER}_*` 变量是否已注入
- `default_tier` 是否能映射到有效 model group
