# 开发与测试

## 范围

本仓库仅维护插件 **0.7.0**，连接独立 MCP **0.5.0**。`plugin.py` 处理命令、Host 可信身份、配置回调及发送，`bridge.py` 处理 MCP 客户端与签名，`delivery.py` 在 Host 数据目录保存发送记录。

不再维护 `service/` 或宿主机安装部署工具；协议、配置字段、任务归属、幂等和发送库格式保持不变。服务与 Codex 登录由独立服务维护者管理，插件生命周期不得管理服务进程。

## 根目录开发环境

从本插件仓库根目录执行，Python 版本须满足根 `pyproject.toml`：

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check --select E4,E7,E9,F plugin.py bridge.py delivery.py scripts/build_bundle.py tests
```

根 `pyproject.toml` / `uv.lock` 管理插件开发依赖。这些是开发命令，不是用户安装 MCP 的步骤，不应修改 MaiBot 已安装环境。定向检查可在 pytest 后指定相关文件；不再使用 `--project service` 或 `service/tests`。

## 回归重点

- Host 可信上下文决定归属，顶层与嵌套身份冲突时拒绝；模型参数不能定义身份。
- 空白名单、未授权用户、缺失身份及配置错误明确拒绝；保存配置和插件重载不启停 MCP。
- 保持 `codex_image_submit/status/result/health` 和签名 `scope/op/args/iat`；兼容服务校验 Bearer、HMAC、操作绑定、时效及归属。
- 原消息派生稳定请求标识；状态、诊断、取图不生成新任务；跨用户、平台和聊天流访问拒绝。
- 发送前持久化意图，`sending/send_unknown` 自动及手动均不重试；已知失败与外部结果未知分别处理，不清库恢复。
- 插件升级保留 Host 身份、原 `data_dir`、config、secret 及服务 `state`；同一服务可查询原任务，不能仅由 health 推断升级兼容。

测试使用隔离目录、协议替身或专用本地 MCP 夹具，不读取真实登录、私密配置、生产任务库或发送库。模拟服务与发送记录器不证明真实 Codex 绘图或聊天收图，独立服务实现自身的回归不属于本仓库范围。

## 构建插件包

```sh
uv run --locked python scripts/build_bundle.py
```

构建器仅生成插件 zip，不生成包含 MCP 的完整源码发行包。核对白名单、归档内容和脚本输出的校验信息；不得包含 service、部署工具、实际 config、secret、登录、任务、发送库、缓存、虚拟环境或 `.git`。构建成功不代表已发布或部署。

## 文档与验证交接

同步核对 manifest、配置示例、插件配置版本、README 和本轮构建行为。实际检查范围和结果记入 [validation.md](validation.md)；上述 Ruff 命令只检查所选错误规则，不代表全部风格规则通过。

测试、独立审查、目标环境部署、真实绘图、聊天收图与用户验收分别绑定版本和环境。旧版测试数字及绘图证据不证明 0.7.0；历史源码与部署资料见 [固定提交](https://github.com/PargosAks/MaiBot-Codex-Image/tree/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272)。
