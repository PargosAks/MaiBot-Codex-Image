# Codex Image 开发约定

本文件补充本插件的长期实现要求；通用授权和工作区边界沿用上级规则。操作入口见 [开发与测试](docs/development.md)，职责划分见 [架构](docs/architecture.md)，版本验证写入对应记录。

## 模块与维护边界

- 本仓库只维护插件。独立绘图服务、Codex 登录、部署工具及 MaiBot 本体不在修改范围内；不要使用历史 `service/` 路径或 `--project service` 命令。
- `plugin.py` 保留工厂、配置和 Host 组件。`host/` 负责生命周期、可信身份、模型与发送；网络等待不持有全局生命周期锁，同代配置、Bridge 与 journal 一起借用和关闭。
- `creation/artist.py` 协调工作，回执、评审和分享各有独立用例；公开类型位于 `creation/contracts.py`。组件接收所需能力，不反向依赖完整 Artist 实例。
- `domain/store.py` 保留稳定门面，schema、共享 SQLite 事务、账本、评审和唤醒按职责组合。跨模块业务必须沿用同一连接、锁和事务；不通过 mixin 或动态代理隐藏事务边界。
- `mcp_client/` 处理签名、协议和回执校验。业务代码依赖公开契约，不调用 Bridge 私有函数。纯图片验证位于 `domain/image_validation.py`，不依赖 Host。
- `workspace/` 只查询和展示。缓存不能跳过每次请求的权限和完整性校验。内部模块使用包内相对导入，适配 Host 的动态命名空间。

## 不变量

- 只信任 Host 原始身份和消息标识；冲突即拒绝。模型参数不能决定 owner、stream 或任务归属。
- 预留、稳定请求标识与原任务查询保证幂等；未知绘图只读恢复，未知发送不能重试。任务终态先落账，再下载图片；下载失败不撤销已确认结果或重新绘图。
- 发送前原子取得持久意图。明确未派发才记 `send_failed`；派发后无可靠回执记 `send_unknown`。只有持有本次意图的调用方可以收尾，手动重发与自主分享遵循各自既有规则。
- SQLite 异步操作使用 `domain/async_work.py`。取消等待者后仍等后台写入结束，才能关闭库或释放状态目录所有权。一个状态目录只允许一个恢复实例；打开和恢复发送库前先取得覆盖手动模式的独占锁，热重载期间保留该锁，关库后才释放。
- 保留配置、密钥、任务、发送记录、累计额度和授权版本；不能清库、改原请求标识或扩大预算来绕过失败。

## 开发、测试与发行

```sh
uv sync --locked
uv run --locked python scripts/check.py
uv run --locked python scripts/check.py --suite unit
uv run --locked python scripts/check.py --suite local
uv run --locked python scripts/check.py --suite external
uv run --locked python scripts/build_bundle.py
```

- 根 `pyproject.toml` / `uv.lock` 管理环境；默认入口包含静态错误检查和 pytest。定向测试可透传路径、`-k` 和报告参数。直接运行 pytest 仍受支持。
- 测试按业务行为组织，复用 `tests/support/` 的替身、数据和 Host 装载器；不能从另一个 `test_*.py` 导入共享实现。合并重复准备过程，保留不同业务边界的断言。
- 固定 Host 契约使用固定 Git 对象或经过指纹核验的公开文件。显式来源错误应失败，缺少可选来源明确跳过；外部契约的严格验收使用 `--require-external`，不能把跳过写成通过。
- 历史迁移夹具从旧版 DDL 建库，不通过删除当前表假装旧库。发行版本断言与元数据核对，历史配置输入仍保留原版本。
- 回归只用临时数据、静态合成图片、模拟模型/发送及本地协议夹具；不读取真实登录、配置、生产任务或图片。公开 Host 契约、模拟集成、部署和用户验收分别报告。
- 修改版本时核对 manifest、pyproject、锁文件及文档；配置版本、DB schema 与插件发行版本分别管理。兼容范围只由实际矩阵证明，不能用单一 SDK 通过概括整个区间。
- 新生产模块必须显式进入 `scripts/build_bundle.py` 白名单，并从解压包验证真实 Host Loader。配置、secret、登录、状态、缓存、测试环境和私密资料禁止进入包。
- 对非显然的授权、事务、取消与恢复规则补充中文注释，解释原因和约束；避免逐行翻译代码。
