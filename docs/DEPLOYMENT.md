# genworker Deployment Guide

## 1. Deployment Model

`genworker` 默认适合以下几类部署方式：

- 本地开发机直接运行
- 单机 Linux 服务进程
- 反向代理后的 HTTP 服务
- 容器化运行

默认推荐从最简单的单进程模式开始，确认 Worker、Skill、配置与日志路径都稳定后，再引入反向代理或外部依赖。

## 2. Local Development

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp configs/config.example.env configs/config_local.env
python start.py
```

## 3. Custom Host / Port

```bash
HTTP_HOST=127.0.0.1 HTTP_PORT=8012 python start.py
```

## 4. Log Directory

建议显式设置日志目录，尤其是在 CI、容器或多实例部署里：

```bash
LOG_DIR=/var/log/genworker python start.py
```

如果 `LOG_DIR` 使用相对路径，它会按项目根目录解析。

## 5. Reverse Proxy

典型反向代理场景：

- TLS 终止放在 Nginx / Caddy / Traefik
- `genworker` 自身只监听内网地址
- 流式接口需要关闭代理缓冲

Nginx 关键点：

- 转发 `Host`、`X-Forwarded-For`、`X-Forwarded-Proto`
- 对 SSE 路径关闭 `proxy_buffering`
- 适当放宽 `proxy_read_timeout`

## 6. systemd Example

```ini
[Unit]
Description=genworker
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/genworker
Environment=ENVIRONMENT=local
Environment=HTTP_HOST=127.0.0.1
Environment=HTTP_PORT=8000
Environment=LOG_DIR=/var/log/genworker
ExecStart=/opt/genworker/venv/bin/python /opt/genworker/start.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## 7. Container Notes

如果你用容器部署，建议：

- 把 `configs/` 作为挂载配置目录
- 把 `workspace/` 作为持久化卷
- 把日志输出到挂载目录或标准输出
- 使用环境变量覆盖运行时开关

最少需要持久化的通常是：

- `workspace/`
- 自定义日志目录

## 8. Optional Dependencies

### 8.1 Redis

适用：

- 会话增强
- 分布式场景
- 更稳定的运行时存储

### 8.2 OpenViking

适用：

- 语义记忆检索
- 向量或派生索引增强

### 8.3 MySQL

适用：

- 更完整的企业部署形态

默认本地模式下，这些依赖都不是启动前提。

## 9. Health Checks

部署时建议同时使用两类探针：

- 存活探针：`/health`
- 就绪探针：`/readiness`

不要只用 `/health` 代替真实就绪判断。

## 10. Startup Validation Checklist

- 配置文件位于 `configs/`
- `config_local.env` 中的 profile 与依赖开关符合预期
- `workspace/` 内默认租户和 Worker 可被加载
- `LOG_DIR` 写入位置符合预期
- `/health` 返回成功
- `/readiness` 返回成功
- `/api/v1/debug/runtime` 中组件状态符合预期
