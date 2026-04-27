# Release Notes

## gw-v0.1.0

发布日期：2026-04-27

### 新增能力

- 本地前端从单一聊天页扩展为聊天入口加 worker 管理台
- 新增 worker 运行总览、自动重载状态、调度与传感器状态展示
- 新增 worker 配置查看页，可直接查看 `PERSONA.md`、`duties/*.md`、`goals/*.md`
- 新增 IM 配置面板，可查看 channel 状态、编辑 channel 绑定并触发运行时重载
- 新增后端接口 `GET /api/v1/worker/ops/config`，用于读取 worker 文件侧配置

### 行为变更

- 本地 Web UI 不再只有聊天窗口，默认同时提供最小管理台能力
- IM 配置保存接口改为支持部分字段更新，前端不需要重复提交已经存在的完整密钥
- 当 `IM_CHANNEL_ENABLED=false` 时，前端会明确提示 IM 配置当前不可用，而不是显示空白页

### 配置变更

- 无新增必填环境变量
- 若需要在页面中启用 IM 配置读写与运行时状态查看，需要设置 `IM_CHANNEL_ENABLED=true`

### 兼容性影响

- 现有聊天入口 `POST /api/v1/chat/stream` 保持不变
- 现有 worker 流式入口 `POST /api/v1/worker/task/stream` 保持不变
- 新增接口是向后兼容扩展，不影响已有调用方

### 验证方式

已完成以下验证：

- `pytest tests/unit/test_im_config_routes.py tests/unit/test_app_factory.py tests/unit/test_runtime_routes.py tests/integration/test_worker_ops_api.py`
- `pytest tests/unit/test_static_frontend.py tests/integration/test_smoke_chat.py`
- `npm run build`
- `python .release/genworker/cli.py verify --work-tree /tmp/gw-out --skip-server-smoke`

### 备注

- 本版本远端发布 commit 对应 `Sourced-From: f3461c865dd51433453ca62443b9e1a82a3f88dc`
