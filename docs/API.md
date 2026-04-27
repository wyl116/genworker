# genworker API Guide

## 1. Overview

`genworker` 默认暴露一组面向本地 Agent Runtime 的 HTTP 接口，核心能力集中在：

- 对话流式输出
- Worker 任务流式输出
- 健康与就绪检查
- 运行时诊断

默认服务地址：

```text
http://127.0.0.1:8000
```

## 2. Core Endpoints

### 2.1 Chat Stream

```http
POST /api/v1/chat/stream
Content-Type: application/json
```

用途：

- 发起或继续一段对话
- 以 SSE 方式接收模型与运行时事件

典型请求体：

```json
{
  "message": "你好，帮我总结今天的任务重点",
  "thread_id": "chat-001",
  "tenant_id": "demo",
  "worker_id": "analyst-01"
}
```

## 2.2 Worker Task Stream

```http
POST /api/v1/worker/task/stream
Content-Type: application/json
```

用途：

- 触发一次面向 Worker 的任务执行
- 适合自动化、集成或外部系统调用

典型请求体：

```json
{
  "task": "检查我的收件箱并整理待办",
  "tenant_id": "demo",
  "worker_id": "analyst-01"
}
```

## 2.3 Health

```http
GET /health
```

用途：

- 进程级存活探针
- 适合负载均衡器或容器探针

## 2.4 Readiness

```http
GET /readiness
```

用途：

- 判断默认聊天主链路是否已经可服务
- 比 `/health` 更接近真实运行态

## 2.5 Runtime Debug

```http
GET /api/v1/debug/runtime
```

用途：

- 查看运行时 profile
- 查看默认 worker
- 查看 Redis / MySQL / OpenViking / IM 开关状态
- 查看当前关键组件的 backend 与降级状态

## 3. Authentication

如果你启用了以下任一配置，API 将进入鉴权模式：

- `API_BEARER_TOKEN`
- `API_KEY`

示例：

```bash
curl -s http://127.0.0.1:8000/api/v1/debug/runtime \
  -H "Authorization: Bearer your-token"
```

## 4. Streaming Notes

`/api/v1/chat/stream` 与 `/api/v1/worker/task/stream` 默认是流式接口。

调用方需要注意：

- 连接会持续到本轮执行结束
- 中途可能收到阶段事件、增量文本和结束事件
- 反向代理需要允许流式透传，不要强制缓冲

## 5. Quick Verification

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/readiness
curl -s http://127.0.0.1:8000/api/v1/debug/runtime
```

## 6. Troubleshooting

### 6.1 `/health` 正常但 `/readiness` 失败

说明进程已启动，但默认运行链路尚未准备完成。优先检查：

- Worker 是否正确加载
- Skill 是否正确加载
- LLM / Session / Memory 依赖是否进入可服务状态

### 6.2 流式接口在代理后不返回增量

优先检查代理层是否启用了响应缓冲。对于 Nginx，这通常意味着需要关闭 `proxy_buffering`。
