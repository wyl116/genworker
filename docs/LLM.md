# genworker LLM Guide

## 1. Overview

`genworker` 的 LLM 接入默认基于 LiteLLM。

这套配置分成两层：

1. 运行时基础配置：通过 `configs/config.env` / `configs/config_local.env` 和进程环境变量加载
2. LiteLLM 路由配置：本地开发从 `configs/litellm_local.json` 读取；正式部署通过外部配置注入，后续可替换为 Nacos provider

公开仓库只会发出本地模板，不会包含真实 provider 密钥或私有 endpoint。

## 2. Shipped Template

公开导出的 `genworker` 默认只带一份 LiteLLM 模板：

- `configs/litellm_local.json.example`

使用时复制为本地私有文件：

```bash
cp configs/litellm_local.json.example configs/litellm_local.json
```

## 3. What The JSON File Controls

`configs/litellm_local.json` 主要定义这几件事：

- `default_tier`: 默认 tier
- `model_list`: LiteLLM 实际可调用的 model group 列表
- `tier_aliases`: 把业务 tier 映射到具体 model group
- `fallbacks`: model group 的降级链
- Router 级参数：如 `routing_strategy`、`num_retries`、`timeout`

## 4. Tier Model

`genworker` 当前只暴露以下四个 base tier：

- `fast`
- `standard`
- `strong`
- `reasoning`

设计意图：

- tier 只表达成本、速度和能力等级
- `requires_tools` 仍由调用点声明，但不再扩散到配置层
- 如果请求命中 `fast` 且需要 tools，routing policy 会自动软升级到 `standard`

## 5. Minimal Local Configuration

### `configs/litellm_local.json`

```json
{
  "default_tier": "standard",
  "model_list": [
    {
      "model_name": "tier-fast",
      "litellm_params": {
        "model": "${LITELLM_FAST_MODEL}",
        "api_base": "${LITELLM_FAST_API_BASE}",
        "api_key": "${LITELLM_FAST_API_KEY}"
      }
    },
    {
      "model_name": "tier-standard",
      "litellm_params": {
        "model": "${LITELLM_STANDARD_MODEL}",
        "api_base": "${LITELLM_STANDARD_API_BASE}",
        "api_key": "${LITELLM_STANDARD_API_KEY}"
      }
    },
    {
      "model_name": "tier-strong",
      "litellm_params": {
        "model": "${LITELLM_STRONG_MODEL}",
        "api_base": "${LITELLM_STRONG_API_BASE}",
        "api_key": "${LITELLM_STRONG_API_KEY}"
      }
    },
    {
      "model_name": "tier-reasoning",
      "litellm_params": {
        "model": "${LITELLM_REASONING_MODEL}",
        "api_base": "${LITELLM_REASONING_API_BASE}",
        "api_key": "${LITELLM_REASONING_API_KEY}"
      }
    }
  ],
  "tier_aliases": {
    "fast": "tier-fast",
    "standard": "tier-standard",
    "strong": "tier-strong",
    "reasoning": "tier-reasoning"
  },
  "fallbacks": [
    {"tier-reasoning": ["tier-strong"]},
    {"tier-strong": ["tier-standard"]},
    {"tier-standard": ["tier-fast"]}
  ]
}
```

配套环境变量：

```bash
export LITELLM_FAST_MODEL=provider/fast-model
export LITELLM_FAST_API_BASE=https://your-provider.example/v1
export LITELLM_FAST_API_KEY=replace-me
export LITELLM_STANDARD_MODEL=provider/standard-model
export LITELLM_STANDARD_API_BASE=https://your-provider.example/v1
export LITELLM_STANDARD_API_KEY=replace-me
export LITELLM_STRONG_MODEL=provider/strong-model
export LITELLM_STRONG_API_BASE=https://your-provider.example/v1
export LITELLM_STRONG_API_KEY=replace-me
export LITELLM_REASONING_MODEL=provider/reasoning-model
export LITELLM_REASONING_API_BASE=https://your-provider.example/v1
export LITELLM_REASONING_API_KEY=replace-me
```

## 6. Fallbacks

`configs/litellm_local.json` 里的 `fallbacks` 用于定义降级顺序：

- `reasoning -> strong`
- `strong -> standard`
- `standard -> fast`

不再维护单独的 `*-tools` fallback 链。

## 7. Non-Local Deployment

`test` / `prod` 环境不再从仓库静态文件读取 LiteLLM 配置。启动前必须注入：

- `LITELLM_CONFIG_SOURCE=json` + `LITELLM_CONFIG_JSON`
- 或 `LITELLM_CONFIG_SOURCE=file` + `LITELLM_CONFIG_PATH`

如果非本地环境未注入，启动会直接 fail-fast。

## 8. Recommended Practices

- 只提交 `configs/litellm_local.json.example`，不要提交真实 `configs/litellm_local.json`
- 在部署系统中通过环境变量或注入文件提供真实值
- 优先通过 4 个 base tier 管理模型能力，而不是在业务代码里写死模型名

## 9. Troubleshooting

### 9.1 Runtime Starts But LLM Is Unavailable

优先检查：

- `configs/litellm_local.json` 是否存在
- JSON 中引用的环境变量是否已经注入
- `tier_aliases` 是否完整覆盖 `fast / standard / strong / reasoning`

### 9.2 Non-Local Startup Exits Early

优先检查：

- `ENVIRONMENT` 是否为非本地值
- `LITELLM_CONFIG_SOURCE` 是否设置为 `json` 或 `file`
- 注入的 JSON 是否是合法对象，且包含完整 tier 映射

### 9.3 How Do I Change Which Model A Request Uses?

不要优先去改业务代码。

优先检查两层：

1. 当前请求会被路由到哪个 base tier
2. 该 tier 在 LiteLLM 配置中映射到哪个 model group
