# IM Channel Runbook

本文档用于部署和联调 genworker 的 IM Channel 子系统，覆盖飞书、企微、钉钉、Slack 四类渠道。

当前实现已经切换到 worker 级渠道凭据模型：

- `PERSONA.md` 只声明渠道绑定与路由信息
- `CHANNEL_CREDENTIALS.json` 负责平台凭据
- 平台 client、sensor、主动发送都按 `tenant_id + worker_id + channel_type` 解析
- 出站 transport 统一收敛到 `src/channels/outbound.py`，由 `WorkerScopedChannelGateway` 在运行时按 worker 解析
- 新代码与排障入口应以 `src/channels/` 和 `src/runtime/channel_runtime.py` 为准，`src/worker/integrations/*_channel_adapter.py` 仅保留历史兼容导入
- 不再回退到全局 `Settings` 平台凭据

## 1. 前置条件

- 服务已启用 `src/bootstrap/channel_init.py`，应用路由已注册 `src/api/routes/channel_routes.py`
- worker channel binding 解析 helper 位于 `src/channels/bindings.py`，供 bootstrap 初始化与局部热重载共用
- 已完成对应平台机器人的创建，并拿到凭证
- 安装依赖：
  - `pip install -r requirements.txt`
  - 飞书长连接额外依赖：`lark-oapi`
  - 钉钉 Stream 长连接额外依赖：`dingtalk-stream`
  - Slack Web / Socket Mode 依赖：`slack_sdk`
- 企微加解密当前依赖系统 `openssl`，需确保命令可用：`openssl version`

## 2. 配置方式

Worker 目录结构：

```text
workspace/
└── tenants/
    └── {tenant_id}/
        └── workers/
            └── {worker_id}/
                ├── PERSONA.md
                ├── CHANNEL_CREDENTIALS.json
                └── runtime/
                    ├── inbox.json
                    └── heartbeat_meta.json
```

说明：

- `runtime/inbox.json` 是自治运行时的事实队列落盘文件，对应 `src/autonomy/inbox.py`
- `runtime/heartbeat_meta.json` 保存主会话 heartbeat 的 cursor、concerns、task refs，对应 `src/autonomy/main_session.py`

### 2.1 `PERSONA.md`

在对应 Worker 的 `PERSONA.md` frontmatter 中声明 `channels`：

```yaml
channels:
  - type: feishu
    connection_mode: webhook
    chat_ids: ["oc_xxx"]
    reply_mode: streaming
    features:
      monitor_group_chat: false

  - type: feishu
    connection_mode: websocket
    chat_ids: ["oc_xxx"]
    reply_mode: streaming
    features:
      verification_token: "evt_token"
      encrypt_key: ""

  - type: wecom
    connection_mode: webhook
    chat_ids: ["chat_123"]
    reply_mode: streaming
    features:
      callback_token: "token123"
      encoding_aes_key: "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
      stream_interval: 1.0

  - type: dingtalk
    connection_mode: stream
    chat_ids: ["cid_xxx"]
    reply_mode: streaming
    features:
      topic: "chatbot.message"

  - type: slack
    connection_mode: webhook
    chat_ids: ["C01234567"]
    reply_mode: streaming
    features:
      update_interval_ms: 500

  - type: slack
    connection_mode: socket_mode
    chat_ids: ["D01234567"]
    reply_mode: streaming
    features:
      update_interval_ms: 500
```

说明：

- `connection_mode`
  - 飞书：`webhook` / `websocket`
  - 企微：`webhook`
  - 钉钉：`webhook` / `stream`
  - Slack：`webhook` / `socket_mode`
- `chat_ids` 用于限制可路由会话
- 同一 `channel_type` 下，同一个 `chat_id` 只能绑定一个 worker；重复配置会在启动或热重载时报错
- `reply_mode=streaming`
  - 飞书：卡片增量更新
  - 钉钉：互动卡片增量更新
  - 企微：按时间间隔分段发送累积 markdown（不是单消息编辑）
- 同一条平台消息会在 Router 层按 `{channel_type}:{message_id}` 去重；Redis 不可用时退化为单实例内存去重

### 2.1.1 低成本跨渠道历史回查

当前 `src/channels/router.py` 已支持一个低成本的跨渠道续聊兜底：

- 默认不做跨渠道检索
- 仅当消息看起来像“继续之前那件事”，且本地 FTS 历史里确实能搜到其他 session 的相关记录时，才会先发确认提示
- 用户明确回复“是”后，才把压缩后的历史摘要拼进 `task_context`
- 该逻辑只发生在入口层，不会把跨渠道检索变成每轮固定成本

可在 `channels[].features` 中使用以下键做调优：

- `cross_channel_lookup_prompt`
  - 自定义提醒文案
  - 默认文案：`这条消息看起来像是在继续之前聊过的话题。如果这是你之前在其他渠道里沟通过的同一件事，回复“是”我可以按关键词回查历史记录。`
- `cross_channel_lookup_markers`
  - 自定义“疑似续聊”触发词，支持逗号分隔字符串或数组
  - 默认包括：`之前`、`上次`、`继续`、`接着`、`还是那个`、`按之前`、`后续`、`跟进`
- `cross_channel_lookup_email_subject_enabled`
  - 是否允许邮件场景按 `subject` 做更敏感的低成本触发
  - 默认 `true`

示例：

```yaml
channels:
  - type: feishu
    connection_mode: webhook
    chat_ids: ["oc_xxx"]
    reply_mode: streaming
    features:
      cross_channel_lookup_prompt: "这件事像是跨渠道延续，回复“是”后我再查历史。"
      cross_channel_lookup_markers:
        - 之前
        - 继续
        - 还是那个

  - type: email
    connection_mode: poll
    chat_ids: ["support@corp.com"]
    reply_mode: complete
    features:
      cross_channel_lookup_email_subject_enabled: true
```

行为边界：

- 该能力依赖 `conversation` 初始化阶段创建的 `session_search_index`
- 只回查同一 `tenant_id + worker_id` 下的原始 session 消息
- 当前是关键词/主题驱动，不做显式 case id 贯通
- 如果用户回复“不是 / 不用查”，入口层会直接回到正常处理路径

### 2.2 `CHANNEL_CREDENTIALS.json`

在同一 worker 目录下创建 `CHANNEL_CREDENTIALS.json`，声明该 worker 自己的渠道凭据：

```json
{
  "feishu": {
    "app_id": "cli_alice",
    "app_secret": "xxx"
  },
  "wecom": {
    "corpid": "ww_alice",
    "corpsecret": "xxx",
    "agent_id": "100001"
  },
  "dingtalk": {
    "app_key": "ding_alice",
    "app_secret": "xxx",
    "robot_code": "robot_alice"
  },
  "slack": {
    "bot_token": "xoxb-xxx",
    "app_token": "xapp-xxx",
    "signing_secret": "xxx",
    "team_id": "T01234567"
  }
}
```

说明：

- 平台字段缺失表示该 worker 未启用该平台
- `PERSONA.md` 声明了某平台，但 `CHANNEL_CREDENTIALS.json` 缺少对应配置时，该 binding 会被跳过
- 同一 worker 同一平台当前只支持一组凭据
- worker channel 路径不会回退到全局环境变量中的平台凭据

## 3. Webhook 与长连接

统一回调路径：

- `GET /api/v1/channel`
- `GET /api/v1/channel/{adapter_id}/status`
- `GET /api/v1/channel/{adapter_id}/webhook`
- `POST /api/v1/channel/{adapter_id}/webhook`
- `POST /api/v1/channel/{adapter_id}/interactivity`
- `POST /api/v1/channel/{adapter_id}/slash`

`GET /status` 返回：

- `healthy`: 当前健康位
- `details.connection_state`: `stopped` / `starting` / `connecting` / `connected` / `reconnecting` / `degraded`
- `details.active_modes`: 当前启用的连接模式集合
- `details.breaker_state`: `closed` / `open` / `half_open`
- `details.reconnect_attempts`: 长连接累计重连次数
- `details.current_backoff_seconds`: 当前退避等待秒数
- `details.next_retry_at` / `details.circuit_open_until`: 下一次重试与熔断恢复时间
- `details.last_error`: 最近一次连接错误
- `details.last_connected_at` / `details.last_event_at`: 最近连接与收包时间
- 飞书额外包含 `websocket_enabled` / `websocket_running`
- 钉钉额外包含 `stream_enabled` / `stream_running`
- 企微额外包含 `webhook_enabled` / `encryption_enabled`

常见 `adapter_id`：

- 飞书：`feishu:{tenant_id}:{worker_id}`
- 企微：`wecom:{tenant_id}:{worker_id}`
- 钉钉：`dingtalk:{tenant_id}:{worker_id}`
- Slack：`slack:{tenant_id}:{worker_id}`

示例：

- `feishu:demo:alice`
- `wecom:demo:bob`
- `dingtalk:demo:ops-assistant`
- `slack:demo:ops-assistant`

当前策略：

- 飞书 `webhook` 始终可用
- 飞书 `websocket` 仅在安装 `lark-oapi` 后启动；缺依赖时保留 webhook 能力，状态检查会反映长连接未启动
- 钉钉 `stream` 仅在安装 `dingtalk-stream` 后启动；缺依赖时仍保留 webhook 入口
- Slack `webhook` 与 `socket_mode` 可以按 binding 独立启用；缺 `signing_secret` 或 `app_token` 时会按 mode 降级，不影响另一条入口
- 企微当前仅支持 webhook 入站，回复端支持完整回复和分段累积式 streaming
- 飞书与钉钉长连接都带指数退避与基础熔断；恢复窗口结束后会进入半开探测
- Slack `reply_stream` 通过 `chat.update` 节流聚合，默认最小更新间隔 500ms
- 默认带轻量抖动，避免多个实例同时重连；可通过 `features.reconnect_jitter_ratio` 覆盖

## 4. 本地验证

### 4.1 飞书 webhook

URL 校验：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/channel/feishu:demo:alice/webhook \
  -H 'Content-Type: application/json' \
  -d '{"type":"url_verification","challenge":"abc"}'
```

消息回调：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/channel/feishu:demo:alice/webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "header": {"event_type": "im.message.receive_v1"},
    "event": {
      "message": {
        "message_id": "om_1",
        "chat_id": "oc_xxx",
        "chat_type": "group",
        "message_type": "text",
        "content": "{\"text\":\"你好\"}"
      },
      "sender": {
        "sender_id": {"open_id": "ou_1"},
        "sender_type": "user",
        "sender_name": "Alice"
      }
    }
  }'
```

### 4.2 Slack webhook

Events URL 校验：

```bash
python - <<'PY'
import hashlib
import hmac
import json
import time

secret = "your-signing-secret"
body = json.dumps({
    "type": "url_verification",
    "challenge": "abc123"
}, separators=(",", ":")).encode("utf-8")
ts = str(int(time.time()))
sig = "v0=" + hmac.new(
    secret.encode("utf-8"),
    f"v0:{ts}:".encode("utf-8") + body,
    hashlib.sha256,
).hexdigest()
print(ts)
print(sig)
print(body.decode("utf-8"))
PY
```

拿到输出的时间戳、签名和 body 后调用：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/channel/slack:demo:alice/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Slack-Request-Timestamp: <ts>' \
  -H 'X-Slack-Signature: <sig>' \
  -d '{"type":"url_verification","challenge":"abc123"}'
```

普通消息回调：

```bash
python - <<'PY'
import hashlib
import hmac
import json
import time

secret = "your-signing-secret"
body = json.dumps({
    "type": "event_callback",
    "team_id": "T01234567",
    "event_id": "EvTest001",
    "event": {
        "type": "message",
        "channel": "D01234567",
        "channel_type": "im",
        "user": "U01234567",
        "text": "你好",
        "ts": "1713686400.000100"
    }
}, separators=(",", ":")).encode("utf-8")
ts = str(int(time.time()))
sig = "v0=" + hmac.new(
    secret.encode("utf-8"),
    f"v0:{ts}:".encode("utf-8") + body,
    hashlib.sha256,
).hexdigest()
print(ts)
print(sig)
print(body.decode("utf-8"))
PY
```

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/channel/slack:demo:alice/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Slack-Request-Timestamp: <ts>' \
  -H 'X-Slack-Signature: <sig>' \
  -d '<body>'
```

### 4.3 Slack slash command

```bash
python - <<'PY'
import hashlib
import hmac
import time

secret = "your-signing-secret"
body = (
    "team_id=T01234567&channel_id=C01234567&user_id=U01234567"
    "&user_name=alice&command=%2Fhelp&text="
    "&response_url=https%3A%2F%2Fexample.com%2Fresp"
    "&trigger_id=1337.42"
).encode("utf-8")
ts = str(int(time.time()))
sig = "v0=" + hmac.new(
    secret.encode("utf-8"),
    f"v0:{ts}:".encode("utf-8") + body,
    hashlib.sha256,
).hexdigest()
print(ts)
print(sig)
print(body.decode("utf-8"))
PY
```

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/channel/slack:demo:alice/slash \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H 'X-Slack-Request-Timestamp: <ts>' \
  -H 'X-Slack-Signature: <sig>' \
  --data-raw '<body>'
```

### 4.2 企微 webhook

明文校验：

```bash
curl -s "http://127.0.0.1:8000/api/v1/channel/wecom:demo:alice/webhook?echostr=hello"
```

加密回调需要带：

- `msg_signature`
- `timestamp`
- `nonce`
- XML body 中的 `Encrypt`

仓库已有单测 `tests/unit/test_im_channels.py` 可作为请求结构参考。

### 4.3 钉钉 webhook / stream

challenge：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/channel/dingtalk:demo:alice/webhook \
  -H 'Content-Type: application/json' \
  -d '{"challenge":"xyz"}'
```

普通消息：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/channel/dingtalk:demo:alice/webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "conversationId":"cid_xxx",
    "senderId":"user_1",
    "senderNick":"Bob",
    "text":{"content":"hi"},
    "msgtype":"text",
    "msgId":"msg_1"
  }'
```

若使用 `stream` 模式，可通过状态端点确认 SDK 客户端是否已启动。

## 4.4 流式回复验证

建议在 `PERSONA.md` 中对目标渠道设置 `reply_mode: streaming` 后验证：

- 飞书：先发卡片，随后内容逐步更新
- 钉钉：先发互动卡片，随后内容逐步更新
- 企微：按 `features.stream_interval` 节流发送累积 markdown；若未到节流窗口，最终会至少发送一次完整结果

若渠道不支持或调用失败，Router 仍会保留非流式路径：先收集完整回复，再走 `adapter.reply()`。

## 5. 运行期操作

查看已注册渠道：

```bash
curl -s http://127.0.0.1:8000/api/v1/channel
```

查看单个渠道健康状态：

```bash
curl -s http://127.0.0.1:8000/api/v1/channel/feishu:demo:alice/status
```

典型返回：

```json
{
  "adapter_id": "feishu:demo:alice",
  "healthy": true,
  "details": {
    "connection_state": "connected",
    "breaker_state": "closed",
    "reconnect_attempts": 1,
    "current_backoff_seconds": 0.0,
    "last_error": "",
    "last_connected_at": "2026-04-08T10:00:00+00:00",
    "last_event_at": "2026-04-08T10:00:05+00:00"
  }
}
```

Worker 配置变更后重载：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/worker/ops/reload
```

以下变更都会触发目标 worker 的局部重载：

- `PERSONA.md`
- `CHANNEL_CREDENTIALS.json`
- `duties/*.md`
- `goals/*.md`
- `rules/directives/*.md`
- `rules/learned/*.md`
- `skills/**/SKILL.md`

重载后会刷新：

- Worker runtime
- worker-scoped platform client cache
- integration channel gateway cache
- IM Channel bindings 与长连接生命周期
- worker sensor registry

## 6. 消息链路检查

入站链路：

1. 平台事件进入 `/api/v1/channel/{adapter_id}/webhook` 或 SDK 长连接回调
2. 适配器转换为 `ChannelInboundMessage`
3. `ChannelMessageRouter` 先按 `channel_type + message_id` 去重
4. Router 根据 `chat_id` 和 `channels` 绑定决定目标 Worker
5. `WorkerRouter` 执行回复或派生后台任务

后台任务通知链路：

1. IM 对话触发 `TaskSpawner`
2. `thread_id` 以 `im:{channel}:{chat_id}:{sender_id}` 形式进入任务 metadata
3. `WorkerScheduler` 发布 `task.completed`
4. `ChannelMessageRouter` 根据 thread 回推 `任务已完成: ...`

## 7. 排障

### 7.1 `GET /status` 为 `false`

优先检查：

- `channels` frontmatter 是否正确加载
- 对应 worker 的 `CHANNEL_CREDENTIALS.json` 是否存在且字段完整
- 飞书 `websocket` 是否安装 `lark-oapi`
- 钉钉 `stream` 是否安装 `dingtalk-stream`
- 仅声明长连接模式但 SDK 未安装时，状态会保持不健康
- 若 `connection_state=reconnecting`，优先结合 `last_error` 判断是鉴权失败、SDK 不兼容还是网络抖动
- 若 `breaker_state=open`，说明已触发熔断，需等到 `circuit_open_until` 后才会再尝试连接

### 7.2 收到 webhook 但没有回复

检查：

- `chat_ids` 是否匹配
- 群消息是否 `@bot`
- 是否被 `monitor_group_chat` 分流到 Sensor 路径
- 平台机器人是否有发消息权限
- 同一 `message_id` 是否已被处理并命中去重；命中时日志会出现 `Duplicate message ignored`
- 若开启 `reply_mode=streaming`，检查对应平台能力是否符合预期：
  - 飞书/钉钉看卡片是否持续更新
  - 企微看是否按节流间隔发送累积消息，而不是等待单次完整回复

### 7.3 企微加密回调失败

检查：

- `callback_token`、`encoding_aes_key`、`corpid` 是否一致
- `openssl` 是否可执行
- 请求中的 `msg_signature`、`timestamp`、`nonce` 是否完整

### 7.4 任务完成没有回推

检查：

- 该任务是否来自 IM 对话
- `thread_id` 是否保留 `im:` 前缀
- `task.completed` 事件是否成功发布
- 对应 `chat_id` 的适配器是否仍注册在 `IMChannelRegistry`

### 7.5 OpenViking 不可用

当前 IM 渠道路由不会因为 OpenViking 不可用而整体中断。

表现：

- webhook / stream 入站、Worker 路由、工具执行、最终回复仍会继续
- `write_episode_with_index` 仍会先把 episode 写入 `workspace/.../memory/episodes/*.md`
- OpenViking 检索或建索引失败时，运行时会降级为“历史记忆为空或变少”，而不是让整条对话链路报错
- 入口层的低成本跨渠道历史回查可能命中更少，因为记忆检索侧会 fail open 返回空结果

排查建议：

- 先看应用日志里是否有 `[MemoryOrchestrator] provider ... failed` 或 `Viking index failed` 警告
- 检查 `OPENVIKING_ENDPOINT` 是否可达，以及 `/health` 是否正常
- 如果 IM 回复正常但历史上下文明显缺失，优先按记忆系统降级处理，不要误判为渠道链路故障

## 8. 当前限制

- 飞书长连接为可选增强；主路径仍以 webhook 为基础
- 钉钉 Stream 依赖官方 SDK，可选开启
- 企微 AES 当前通过系统 `openssl` 实现，不是官方 Python SDK 封装
- 企微 streaming 是“多条累积消息”体验，不是原地编辑
- 去重 fallback 仅保证单实例内有效，跨进程场景仍依赖 Redis
- 目前已提供指数退避、轻量抖动和基础熔断，尚未引入熔断分级和集中观测指标
- OpenViking 当前是“检索/索引派生层”，不可用时会降级为弱历史上下文，而不是阻断 IM 主链路
