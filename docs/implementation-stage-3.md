# 小画师阶段 3 只读恢复

阶段 3 增加独立 MCP 的原请求查询，以及插件 Bridge 和本地操作账本的恢复对接。远端查询只读，本地按经过核对的回执更新原操作；不会创建新的绘图或发送请求。自主创作运行器仍未接入生产 Host。

## 代码边界

独立 MCP 源码维护于工作区 `codex-image-service`，从历史提交 `e50a7b6` 的 0.5.0 提取，具有自己的 Git 历史。没有修改原 `codex-image-mcp` 安装目录，也没有将服务或部署代码加回插件包。MaiBot 本体和真实配置保持原样。

| 模块 | 本轮职责 |
| --- | --- |
| 独立 MCP store / service / server | 按签名归属及 request_id 只读查原任务，声明 lookup_v1，返回结构化错误码 |
| bridge.py | 探测能力、透传错误码、签名 lookup、核对原请求与任务绑定 |
| recovery.py | 显式调用只读查询，将已确认结果对账到原 ArtistStore 操作，不创建绘图或分享 |

## 能力和错误

Bridge 每次恢复先读取 health 的 capabilities；缺失表示旧服务不支持，返回 `LOOKUP_UNSUPPORTED`，不盲猜接口。阶段 3 服务仅声明 `lookup_v1`，没有原图编辑能力。

`BridgeError` 继承 ValueError，保留 `.code` 与 `.kind`。规范的远端 `error.code` 原样透传，包括未来未知错误码；异常正文不复制到用户消息。旧服务只有 isError 而无代码时为 `REMOTE_ERROR_UNKNOWN`；超时、断线或会话失败为 `TRANSPORT_UNKNOWN`；无法解析的响应为 `INVALID_RESPONSE`。取消继续传播，Bridge 不自动重试。

明确拒绝也只描述本次调用，不证明同编号的此前请求未受理。`REQUEST_CONFLICT`、`QUEUE_FULL` 等不能自动释放历史未知占用或授权补画。

## 查询与恢复契约

`Bridge.lookup(scope, request_id, expected_operation=..., expected_digest=..., expected_job_id=...)` 使用 `op=lookup`，签名参数仅有 request_id。expected 参数来自本地冻结记录，不发给 MCP 作为查询条件，也不能由模型决定。

找到时校验 operation、request_id、request_digest、完整任务结构及已知 job_id；不一致返回 `RECEIPT_MISMATCH`，不绑定其他任务。查询不到返回 `{"found":false}`，保留原请求、参数、已知任务编号与占用，未决操作保守标记为 unknown。

`ReadOnlyRecovery(store, bridge).reconcile(space_id, scope, request_id)` 只处理本地已尝试的绘图操作，返回本地操作快照；BridgeError 保留 code/kind 原样向上传播。可信调用方必须选择与原 service binding 对应的 Bridge，不能由模型提供新服务地址。全部 SQLite 操作在线程中执行，取消时等待已经启动的数据库调用结束。恢复不依赖新增绘图授权，仍要求原空间归属；撤权不抹去已有结果。

queued、starting、running 与产物可用均不等于完成；明确 completed / failed 才结算对应原操作，outcome_unknown 保留未知占用。artifact_ready 还需看 turn_status：completed 为成功，failed/interrupted/cancelled 为已受理失败；仍在运行时保留 accepted，缺失或不明确时保留 unknown，已有图片不删除。相同查询可重复执行，已结算操作直接返回本地记录，不会重放生成、取图发送或改变冻结参数。它没有接入 Artist 的真实绘图端，也不会在插件导入、加载或重载时自动运行。

status=completed 与明确的非 completed 回合状态互相矛盾，Bridge 与本地恢复层均拒绝结算并保留未知占用。旧服务 completed + null 仍可读取；failed + completed 可能表示回合结束但未得到有效图片，不误判为成功，artifact_ready + failed 也保留已受理失败语义。

## 验证方式

插件根测试使用自包含协议替身，独立服务执行自己的真实数据库和模拟驱动测试。跨仓库集成测试另行设置 `CODEX_IMAGE_TEST_SERVICE_SOURCE` 指向独立服务的 `src`，使用同时具备两端测试依赖的解释器，运行 `tests/test_mcp_recovery_integration.py`；默认插件测试不依赖服务源码目录。

联调用例通过真实 Streamable HTTP 验证：服务已经受理但客户端丢失回执后，只查询找回原任务；查不到不补画；相同编号参数冲突及摘要不符不退款。所有数据是临时合成数据，服务 worker 在该用例中停用，以明确核对查询没有启动生成或改动远端数据库。

## 本轮验证

2026-10-31，插件基线 `cba7627`，独立 MCP 提取基线 `0cb4ba7`。Windows、CPython 3.13.18、MCP SDK 1.30.0、httpx 0.28.1、pytest 9.1.1；插件环境 SDK 2.16.0，服务测试环境另有 Pillow 12.3.0 与 tomlkit 0.15.2。复用已有开发解释器但显式加载新仓库源码，未安装或更改运行环境、依赖或锁文件。

| 验证层次 | 实际结果 |
| --- | --- |
| 插件主线完整回归 | 328 passed，2 skipped（16.00s）；两个跳过项为需要显式服务源码的跨仓库用例 |
| 独立 MCP 主线完整回归 | 133 passed（6.47s），包含数据库、签名、模拟 Codex、状态所有权与真实 MCP 传输 |
| 跨仓库联调 | 2 passed（1.17s），显式加载新独立服务，真实本机 HTTP/MCP 传输 |
| 静态检查 | 两端 Ruff `E4,E7,E9,F` 通过 |
| 独立审查 | PASS；首轮实际运行插件 317 项（含跨库）和服务 133 项，复现矛盾回执问题；修复后的固定快照 02 再运行 74 项定向测试及原 11 个独立探针，全部通过 |
| 打包及文件边界 | 插件白名单 24 文件，源码逐字节一致，相对文档链接有效，不含服务、配置私密数据或运行状态 |
| 部署 / 真实模型绘图 / 实际聊天收图 / 用户验收 | 均未执行 |

主线 XML 位于插件忽略目录 `dist/validation/stage3-full-02.xml`、`stage3-integration-02.xml` 和独立服务的 `dist/validation/stage3-full-01.xml`。双仓库 70 文件固定快照为插件目录下 `dist/validation/stage3-review-02`，逐文件摘要清单 SHA256 为 `16b72b39a8e8564a63cadf5ee264bb9c49138c7daa1d7edd3d0b0406d073792e`。验证文档后续补记不改变冻结代码、测试与构建文件。

独立 MCP 尚未配置 GitHub 远端；它的源码与提交和插件仓库分别维护。本轮不部署、不接入真实模型、绘图或发送；这些模拟与协议检查不代表运行环境已经兼容。

独立复核覆盖并发只读查询、跨归属隔离、迟到响应不覆盖终态，以及重复取消时等待数据库工作线程结束；复核前后 70 文件摘要不变，服务源码与首轮一致，未无故重复服务全量。审查记录位于忽略目录 `dist/validation/reviewer-stage3-cb8a49800eb74a2ab6b75586672b59b3/review-result.json`。首轮服务测试曾出现一条 Windows 异步管道关闭警告，无失败断言；未将其当作真实运行环境通过的依据。
