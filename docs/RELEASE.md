# genworker Release Guide

## 1. Scope

这份文档说明如何对 `genworker` 做版本发布、变更校验和仓库维护。

它面向两类人：

- 直接维护 `genworker` 仓库的维护者
- 从上游代码树导出并发布 `genworker` 的维护者

## 2. Release Checklist

发布前至少检查以下内容：

1. 关键 README 与配置文档是否已更新
2. `configs/` 模板是否与当前默认运行形态一致
3. 快速启动命令是否仍然有效
4. 关键测试是否通过
5. 导出产物中是否包含预期文档与配置模板

## 3. Recommended Verification

在仓库根目录执行：

```bash
pytest tests -q
```

如果你维护的是从上游仓库导出 `genworker` 的发布链路，则应在上游仓库中额外验证导出结果：

```bash
python .release/genworker/cli.py sync \
  --source-root . \
  --manifest .release/community-manifest.yml \
  --out /tmp/gw-out \
  --source-ref "$(git rev-parse HEAD)" \
  --remote-url <your-remote-url> \
  --branch main \
  --skip-server-smoke
```

## 4. Versioning

建议使用显式 tag，例如：

```text
gw-v0.0.2
gw-v0.1.0
```

这样更容易和上游主仓库的版本语义区分开。

## 5. Release Notes Structure

建议每次发布说明至少包含：

- 新增能力
- 行为变更
- 配置变更
- 兼容性影响
- 验证方式

## 6. If You Maintain An Upstream Export Flow

如果你的 `genworker` 是从更大的上游仓库导出出来的，建议：

- 把 `README`、配置模板和发布文档维护在独立 overlay 中
- 不要默认复用上游主 README
- 导出后再检查一次最终产物，而不是只检查源文件
- 如果每次导出都会重建 Git 历史，推送时使用受保护的强制更新策略，例如 `--force-with-lease`

## 7. Minimal Publish Flow

对于基于导出链路的维护者，最小发布流程通常是：

1. 更新代码、文档和配置模板
2. 跑最小测试集
3. 导出到临时目录
4. 检查导出仓库内容
5. 推送到目标远端
6. 打 tag 并补发布说明
