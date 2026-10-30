# 小画师阶段 1 状态与规则底座

阶段 1 为后续自主创作提供持久状态、授权、预算和保守迁移，不接入聊天、定时、模型、绘图或发送。`plugin.py`、`bridge.py`、`delivery.py` 的运行路径保持原样；新模块随源码和插件包提供，但当前不会在加载时创建新库或迁移真实数据。

完整架构及后续 MCP 契约见 [小画师设计](artist-design.md)，环境与部署核实边界见 [阶段 0](artist-stage-0.md)。

## 信任边界

授权记录必须由后续可信 Host 适配层建立，记录真实归属和明确的持续委托来源。Python 数据类型只能验证字段结构，不能证明数据来自 Host；不得把模型生成的 scope 或任意配置字典直接登记为授权。白名单仅是入口限制，不代替持续委托。

空间归属不可替换。撤权保留历史与未决额度；重新授权必须显式登记，旧操作的授权版本不会自动更新。本次触发可以没有消息编号，不能把历史授权消息伪装成本次触发。

## 持久化和预算规则

全局与空间额度同时满足才能预留；未设置额度时默认拒绝。预算按 Asia/Hong_Kong 的自然日归档，使用固定 UTC+8 避免引入 Windows 时区数据依赖。思考、绘图和分享分别计数；submit/edit 共用绘图额度，edit 另计同一作品的累计修订上限。第一阶段每次操作固定一个单位。

同一次预留原子写入操作和预算占用，参数、owner、服务绑定及 request_id 冻结。多连接使用数据库事务仲裁，不能只用进程内锁。先预留后提交时必须再次复核授权版本和当前限制；预留不是永久执行许可。额度缩小不能让旧计划按旧限制继续执行。

同一作品存在 reserved/submitting/accepted/unknown 绘图时，新的 submit/edit 返回 WORK_BUSY，即使总额度尚有余量。重复同请求只返回原记录，不再次占用或启动。思考与评审建议可独立按类别预算处理。

未知预留跨日保留，不自动退还；已受理失败按尝试消耗。只有确定未发出的操作或没有先前未知调用的受理前拒绝可以释放。只读查询未找到、编号冲突、网络超时不构成释放证据。

重启恢复只更新本地执行认知，不重放任何外部请求。普通第二连接打开数据库不能被当作崩溃恢复，否则会误改仍在执行的第一连接任务。未来接入运行流程时，恢复操作须由持有独占生命周期权的协调器执行。

## 旧发送库迁移

迁移必须在旧写入者停止后单独执行，本阶段只使用合成数据库测试。源库采用只读打开和 SQLite 备份，不凭空创建缺失的源库，不覆盖已有备份。完整验证源结构、状态及数据库完整性后事务导入；失败不能半导入。

保留原 job 和原 state。sending 按 send_unknown 保护，目标未知的记录保持 job 级锁，不绑定当前聊天。sent、send_failed 也保留旧记录并阻止新自主流程将其当作首次分享；人工发送能力仍由旧运行路径独立处理。

重复迁移不解除锁，未知记录不因重开或重导入回到未发送。不能清空旧库、清锁、用迁移失败的新空库覆盖历史，或把迁移测试等同真实数据已升级。

## 验收范围

必须覆盖：真实 SQLite 多连接竞争；全局和空间额度同时约束；跨日未知占用；修订上限；撤权和预算缩小；参数幂等与冲突；跨空间读取与更新拒绝；重启恢复；备份保真；重复迁移；未知 schema、损坏源库或非法状态的事务拒绝；未来 schema 拒绝加载。

完整灵感库、版本评审、唤醒调度、分享状态机及 Host 授权入口属于后续阶段。这里的操作状态是本地执行账本，不是完整作品状态机。

## 离线 API

调用者使用 `policy.Scope`、`Delegation(source_ref, granted_by, continuous=True)` 和 `Limits`。`ArtistStore(path, clock=...)` 只操作指定数据库；clock 只供可信应用和测试注入，不接受模型指定的预算日期。新库 schema 为 1，不接管未知旧结构或未来版本。

| API | 行为 |
| --- | --- |
| create_space / grant / revoke | 固定真实 scope 和服务绑定，追加授权版本与来源，默认未授权 |
| set_global_limits / set_limits | 分别保存全局和空间限制，重开数据库不重置 |
| create_work / get_work | 保存空间内作品编号和累计修订尝试；未实现完整计划内容 |
| reserve | 冻结操作参数、触发来源和授权版本；事务预留固定一个单位，重复同请求返回原记录 |
| mark_submitting | 再次检查授权版本、预算、分享锁；一次性领取外部调用资格 |
| mark_accepted / mark_unknown | 记录真实受理结果或不确定性；绘图受理需绑定原 job_id |
| cancel_reservation | 仅取消尚未开始的本地预留 |
| reject | 仅允许确定的受理前拒绝码释放当前提交；冲突保留占用 |
| settle / reconcile | 记录已确认结果；未知只能通过带证据的对账处理，不能退回可重提状态 |
| recover_inflight | 旧推进者停止后显式恢复在途记录为未知；普通打开不会调用 |
| check_share / legacy_lock | 检查本空间原图归属、重复分享和旧记录锁；不发送图片 |
| get_operation / audit_operations / audit_authorizations / budget_usage | 读取指定空间的记录和用量，不暴露其他空间 |
| migrate_legacy_deliveries | 离线、只读源库、独立备份、事务导入旧发送锁；不改变运行路径 |

Limits 包含 `think/render/share` 每日尝试上限、所有类别合计的 `max_inflight` 未决上限，以及每作品 `max_revisions`。全局与空间限制均检查；零值不表示无限额度。数值是执行次数限制，不是实际账单估算。

每次迁移使用新的备份路径；已存在路径明确拒绝覆盖。重复导入同一份内容不会新增重复记录或解除旧锁，之前的备份仍保留。`offline=True` 是维护方确认旧写入者已停止的前置声明，不是自动停止进程的能力。

## 本轮验证

日期 2026-10-31，插件 Git 基线 9e3112a 上的未发布工作树，Windows / Python 3.13.18 / SDK 2.16.0。环境没有新增依赖；uv 不在当前 PATH，使用已存在的根 .venv 解释器。

| 验证层次 | 结果与边界 |
| --- | --- |
| 实现作者定向测试 | 41 passed，真实临时 SQLite、多连接预算竞争、授权、未知恢复与迁移 |
| 主线新增独立边界测试并集成 | 44 passed；增加同请求竞争后单次启动、WAL 未 checkpoint 备份和目标中途写失败回滚 |
| 完整插件回归 | 95 passed，包含原有协议替身、发送锁、权限和打包回归；不是生产 MCP 或真实 Host 验收 |
| 静态检查 | Ruff E4/E7/E9/F 通过；实现作者另做新增模块 E/F/B 检查 |
| 独立审查 | PASS；固定 31 文件快照前后哈希一致，独立运行 44 项阶段 1 和 2 项打包测试通过，另有 6 组独立边界验证通过 |
| 部署和真实数据迁移 | 未执行 |
| 真实绘图、看图评审和聊天收图 | 未执行 |
| 用户验收 | 未完成 |

复现本轮主线检查（basetemp 必须使用新的隔离目录，pytest 会清理该目录）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
& ./.venv/Scripts/python.exe -m pytest -p no:cacheprovider -q --basetemp=dist/validation/artist-stage1-new-run --junitxml=dist/validation/artist-stage1-new-run.xml
& ./.venv/Scripts/python.exe -m ruff check --select E4,E7,E9,F plugin.py bridge.py delivery.py policy.py store.py scripts/build_bundle.py tests
```

本地产物位于忽略的 dist/validation：artist-stage1-author/author-final.xml、artist-stage1-integration-01.xml、artist-stage1-full-01.xml。测试仅使用合成来源、临时数据库及原有静态协议夹具；实际配置、登录、聊天和发送库没有参与。

独立审查快照为 artist-stage1-review-01，manifest SHA256 为 f7e0af8339ec74eec4cbe9cb78d8763fd9cd4517d6581967ecb0c32e02c9175d。审查结果与独立脚本保存在 artist-stage1-review-tests/independent-6628a31ebd1346668466f22516ef677e。审查后只补充环境与验证文档，代码及测试按快照再次核对。阶段 0 仍缺运行进程与静态启动入口的绑定证据；不把安装元数据称为部署验收。
