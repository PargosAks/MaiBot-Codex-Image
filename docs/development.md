# 开发与测试

## 当前源码布局

根 `plugin.py` 保持 Host 入口；内部模块迁至 `domain/`、`creation/`、`host/`、`inspiration/`、`mcp_client/`、`workspace/`，页面为 `assets/studio.html`。职责与旧文件映射见[架构说明](architecture.md)。下方阶段沿革中的旧根文件名表示原实现阶段；新代码与测试使用对应子包路径。测试不导入 MaiBot 本体作为运行依赖。

## 测试入口与分层

从仓库根执行 `uv sync --locked`，随后使用同一个入口：

```sh
uv run --locked python scripts/check.py
uv run --locked python scripts/check.py --suite unit
uv run --locked python scripts/check.py --suite local
uv run --locked python scripts/check.py --suite external
uv run --locked python scripts/check.py --no-lint --suite unit tests/test_store_components.py
```

默认包含静态错误检查和全部 pytest 用例；`--no-lint` 用于已做静态检查后的定向回归。pytest 原入口和 `-k`、`--junitxml` 等选项保留。`unit` 包含真实临时 SQLite 与内存效果替身；`local` 使用回环 MCP 服务或 Node；`external` 验证显式提供的公开 Host、SDK、适配器或独立服务源码。它们都不使用生产状态或真实绘图。

外部源码未提供时明确跳过。严格验证某个已准备好的外部契约，可加 `--require-external` 并指定对应文件；任何外部用例跳过都会使结果失败，避免把未执行当通过。选全部 external 前需准备下表的全部依赖。

共享 fixture、模拟器、数据与公开 Host 装载器集中于 `tests/support/`；用例仍按功能放在原模块，不把所有测试合成一个大文件。历史 v7 迁移使用冻结 DDL，固定 Host 文件通过 Git 对象或指纹核验。新增测试复用 support，并声明依赖所属套件。

## 0.10.0 小画师工作区

工作区与聊天摘要仅查询已有账本；档案写入由 Artist 处理。分别运行 `tests/test_studio*.py`、`tests/test_autonomous_catalog*.py`，再用 `tests/test_artist_studio_e2e.py` 验证从建档到原图、细化、评审、分享以及只读查询的联动。用临时数据和模拟外部效果，勿指向运行实例的数据目录。

公开源码契约测试使用以下可选环境变量：

| 变量 | 对象 |
| --- | --- |
| `CODEX_IMAGE_TEST_HOST_REPO` | 含固定提交 `062d45cf` 的公开 MaiBot Git 仓库 |
| `CODEX_IMAGE_TEST_HOST_SOURCE`、`CODEX_IMAGE_TEST_STUDIO_HOST_SOURCE` | 对应 Host 的公开 `src` 目录 |
| `CODEX_IMAGE_TEST_HOST_SITE_PACKAGES` | 已安装的公开 MaiBot 包的 site-packages，或含 `src/plugin_runtime/runner/plugin_loader.py` 与 `pyproject.toml` 的公开 Host 源码根（如 `/MaiMBot`）。源码根没有 MaiBot 安装元数据时，读取同一根目录的 `[project]`，要求 `name` 为 `MaiBot`（不区分大小写），`version` 为合法版本字符串。仅提供 SDK 不足以运行 Loader 检查；显式路径或元数据无效时测试失败。 |
| `CODEX_IMAGE_TEST_SDK_SOURCE` | 包含公开 SDK 2.17 的 site-packages；插件开发环境为 SDK 2.16 |
| `CODEX_IMAGE_TEST_DASHBOARD_SOURCE` | 含 `src/` 的 Dashboard 源码目录，页面桥接测试使用 Node |
| `CODEX_IMAGE_TEST_SERVICE_SOURCE`、`CODEX_IMAGE_TEST_SNOWLUMA_SOURCE` | 独立 MCP 的 `src` 目录及公开适配器源码快照 |

这些检查只加载指定的公开代码。它们不需要访问 VPS 或使用真实聊天、登录态与订阅。

原生编辑 IPC 契约还需要独立服务仓库中与 `src/` 相邻的 `tests/fake_codex.py`，不能只提供生产源码快照。缺失该模拟 CLI 时立即报告依赖缺失。SnowLuma 超时契约同时需要上述 Host 与 SDK 公开来源；严格检查应完整设置依赖，不能把部分跳过视为通过。

## 范围

本仓库维护小画师插件，基础命令兼容独立 MCP **0.5.0**。`plugin.py` 处理 Host 入口，`host/` 负责运行代与发送，`mcp_client/bridge.py` 处理 MCP 客户端与签名，`domain/delivery.py` 在 Host 数据目录保存发送记录。后续能力按 [迭代计划](artist-iteration-plan.md) 分阶段扩展，不修改 MaiBot 本体。

小画师阶段 0/1 新增 `store.py` / `policy.py` 离线底座；阶段 6 由受控 Host runtime 加载，不导入旧发送库或迁移服务数据。架构见 [设计](artist-design.md)，实际 API、隔离测试与未接入边界见 [阶段 1](implementation-stage-1.md)，环境版本见 [阶段 0](artist-stage-0.md)。后续异步运行接入 SQLite 时必须通过工作线程调用，不能阻塞 MaiBot 事件循环。

阶段 2 新增 `artist.py` / `thinking.py`，SQLite schema 1 事务升级至 2。运行器要求显式注入模拟后端，所有 SQLite 调用使用工作线程；当时未接入 Host；阶段 6 仅在显式启用和授权后启动。详见 [阶段 2](implementation-stage-2.md)。

阶段 3 新增 `recovery.py`，Bridge 支持能力探测、错误码和只读 lookup；恢复入口须显式调用，不启动真实创作。独立服务位于单独的 `codex-image-service` 仓库，未重新加入插件。跨仓库联调说明见 [阶段 3](implementation-stage-3.md)。

阶段 4 新增 `editing.py`，显式提交原图编辑并取回同一任务的子版本；Bridge 严格验证编辑回执和图片摘要，状态库绑定父版本。该阶段自主运行器仍只接受模拟后端，详见 [阶段 4](implementation-stage-4.md)。

阶段 5 新增 `rendering.py`，将 MCP 接到 Artist 的绘图协议，账本写入仍只由 Artist 协调。网络绘图需显式 `allow_network_renderer=True`；阶段 6 新增独立 `allow_host_sender=True` 门禁，并由受控 Host runtime 启动。完整模拟链路和故障恢复见 [阶段 5](implementation-stage-5.md)。

不再维护 `service/` 或宿主机安装部署工具；保留原命令协议、任务归属、幂等和发送库格式；新增配置默认关闭。服务与 Codex 登录由独立服务维护者管理，插件生命周期不得管理服务进程。

## 根目录开发环境

从本插件仓库根目录执行，Python 版本须满足根 `pyproject.toml`：

```sh
uv sync --locked
uv run --locked python scripts/check.py
```

根 `pyproject.toml` / `uv.lock` 管理插件开发依赖。这些是开发命令，不是用户安装 MCP 的步骤，不应修改 MaiBot 已安装环境。定向检查可在 pytest 后指定相关文件；不再使用 `--project service` 或 `service/tests`。

## 回归重点

- 真实单作品验收的通用观察入口为 `scripts/acceptance_observer.py`，定向回归为 `tests/test_acceptance_observer.py`，沿用 `scripts/check.py --suite unit`。它是维护工具，不进入插件发行包。观察到 render `unknown`、已有图片或暂时没有领取记录，均不能触发重载；必须等待原任务终态、同图成功评审、作品状态更新及领取退出。超时、读库失败或模型 unknown 仅保留现场，不在异常分支自动暂停、恢复配置或重试。
- 本观察器用于明确限定的单作品、三次思考验收（立项、继续绘图、原图评审）。运行助手必须在启动前通过实际 Host 设置限额、固定香港额度日和观察时限，收尾前确认额度已耗尽，避免空闲采样后又启动新规划。还须先核对原图 SHA、冻结评审快照、授权版本、原请求绑定和当前生效配置，再调用暂停；收到 Host 实际应用成功回执后才恢复生产。不能只用图片、评审行或一次 `active_claims=0` 代替这些条件，也不能自动核销未知记录以使验收通过。
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

`tests/test_package_layout.py` 从发行包解压出独立目录，用隔离 Python 子进程和真实 Host `PluginLoader` 验证包导入、任意插件命名空间、组件/配置/HTML注册、卸载及重新加载；运行时网络和外部进程调用被测试审计拒绝。Runner仅执行公开源码中选定的注册方法，注册接收端为内存替身，不是实际Host进程或IPC部署验收。缺少公开Host源码时明确跳过，不用自造Loader代替。

## 文档与验证交接

同步核对 manifest、配置示例、插件配置版本、README 和本轮构建行为。实际检查范围和结果记入 [validation.md](validation.md)；统一入口中的 Ruff 只检查 `E4,E7,E9,F` 错误规则，不代表全部风格规则通过。

群灵感与独立分享权限的[编号验收记录](group-inspiration-validation.md)配有[脱敏JSON摘要](validation/group-inspiration-20261104.json)。摘要保留测试计数、跳过原因及已测源码指纹；私有日志、配置、数据库、图片和原始JUnit留在维护环境，不随提交上传。文档整理后的包与历史部署包分别注明文件数和适用范围，不以新包摘要替换旧部署证据。

测试、独立审查、目标环境部署、真实绘图、聊天收图与用户验收分别绑定版本和环境。旧版测试数字及绘图证据不证明当前工作树；历史源码与部署资料见 [固定提交](https://github.com/PargosAks/MaiBot-Codex-Image/tree/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272)。

## Host 自主链路

图片发送等待的回归见 `tests/test_send_timeout.py`：通过真实 SDK、公开 Host RPC bridge 的 AST 与临时 SQLite，模拟慢上传、取消和迟到回执。模型等待与发送等待独立；未知操作的发送锁和预算保持不变。`tests/test_snowluma_timeout.py` 是可选公开源码测试：设置 `CODEX_IMAGE_TEST_SNOWLUMA_SOURCE` 指向只含公开源码快照的目录，其中需有 `adapter_transport.py`（Snowluma adapter 1.1.9 的 `transport.py`）及 `host_component_timeout.py`（MaiBot 1.3.15）。测试只提取动作等待/echo 解析函数及超时解析器，替换 websocket、网关传输和上传，不导入插件启动入口，不读取配置、密钥或生产数据库；时间按 100:1 缩放。这不能替代 VPS 部署与 QQ 真实发送验收。

保留已有计划、旧授权与累计额度的维护边界见 [等待恢复](retained-plan-recovery.md)。对应回归为 `tests/test_planned_wait_restore.py`，包括实际 SQLite 状态、正常等待与未知操作拒绝，以及模拟创作、细化、分享和 Host 生命周期。

阶段 6 的 `host_runtime.py` / `host_adapters.py` 对接实际命名 Hook 与 SDK 能力。Host 公共源码定位和严格隔离的验证方法见 [阶段 6](implementation-stage-6.md)。测试入口为 `tests/test_host_lifecycle.py`、`tests/test_host_adapters.py`、`tests/test_host_integration.py`；没有真实模型、绘图或发送。

## 群灵感簿的隔离验证

在插件开发环境运行 `python -m pytest -q tests -k test_group_`（见上方 Host 源码环境变量）。群消息、实验凭据与模型输出都在测试中构造；测试使用临时 SQLite，不读取运行实例的聊天或绘图数据。不要为测试启用生产自主创作。过滤实验与当前念头接口见[群灵感簿设计](group-inspiration.md)。

新的纯插件念头路径使用 `test_group_thought_host.py` 执行固定 Host 的两个规划 Hook、SDK Runner 与 Context Item 序列化，并与真实 SQLite 和只读工作区联通。`test_group_thought_runtime.py` 覆盖直接调用不写入、重放、跨群、撤权及取消等待；`test_group_thoughts.py` 验证独立存储与频控。模型输出均为脚本构造，不能据此声称真实模型会主动选择记灵感。旧 `group_ingress/group_collection` 仍只有合成凭据入口，不能用于绕开过滤。

第二阶段用 `test_group_thought_adoption_store.py` 验证 schema 8 迁移、来源隔离、采用与立项的事务一致性及授权版本；`test_group_thought_sync.py` 验证源记录已提交但未导入时的重载恢复、开关屏蔽和关停等待。`test_group_thought_adoption_flow.py` 将真实 Host Hook/SDK 与脚本模型、静态 PNG、模拟发送串联，覆盖聊天和无人时段的等待／采用、原图细化、预算上限及 `send_unknown` 不重放。只读采用关联和页面文本由 `test_group_thought_studio.py` 验证，不能代替浏览器或 QQ 用户验收。

第三阶段用 `test_group_share_policy_store.py` 覆盖 schema 9、独立分享许可、预留权限版本及历史额度保护；`test_group_share_flow.py` 在实际插件生命周期和 Host/SDK 夹具中验证“仅收藏仍可创作”“模型忽视提示也不能越权发送”“允许原群分享仍可等待”，以及最终群绑定检查、权限撤销、未知发送恢复和私聊兼容。`test_group_share_boundary.py` 包含配置、模型上下文边界及 100:1 缩时的群核验等待测试，确保不挤占原上传等待窗口；实时分享策略不写入冻结的视觉评审快照。`test_group_share_studio.py` 验证管理员只读许可展示及跨群隔离，聊天摘要不自动获取群管理策略。
