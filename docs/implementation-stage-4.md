# 小画师阶段 4：原图编辑

阶段 4 完成原图编辑的服务协议和插件内部组件，不增加聊天命令或自动运行入口。插件保持 0.7.0 开发线，独立服务基于 0.5.0 开发线；新增能力以 `edit_v1` 探测，版本数字不能代替能力声明。旧服务仍可用于原命令，缺少能力时拒绝编辑。

## 模块和调用边界

| 位置 | 职责 |
| --- | --- |
| 插件 `editing.py` | 从本空间、本作品已保存版本取得原图任务和摘要，预留授权预算，显式提交一次，查询原任务并收集子版本 |
| 插件 `bridge.py` | 每次探测能力，签名 edit，验证请求/任务回执；按 lookup 元数据校验 result 的 MIME、长度和 SHA256 |
| 插件 `store.py` | edit 版本的 parent 必须匹配冻结的 source_job_id/source_sha256，版本不覆盖 |
| 独立服务 store/service/images | 事务迁移、跨操作幂等、源图归属与完整解码、冻结字节、执行前复核 |
| 独立服务 codex/server | 注册签名 edit 工具，将受控原图文件作为 `localImage` 输入官方 App Server |

可信适配器负责从 Host 取得 Scope，并选择空间原 service binding 对应的 Bridge；模型不能指定身份、服务地址、任意源任务、URL 或路径。`plugin.py` 未注册这些新入口，阶段 2 Artist 的 simulation 限制保留。

显式 API 为 `ImageEdits(store, bridge).submit(space_id, scope, work_id, source_version_id, request_id, prompt)`，返回本地 operation；`collect(space_id, scope, request_id, source_version_id)` 返回不可变 version 或 `None`。新编辑需要有效授权、额度及修订次数，提交前再次校验；撤权后仍可只读取回原结果。

## MCP 契约

工具名 `codex_image_edit`，签名 `op=edit`。args 恰好包含 request_id（1–200 字符）、source_job_id、source_sha256（64 位小写十六进制）、prompt（非空白，原始长度 1–4000 字符）。不修剪或改写 prompt。

```json
{"request_id":"edit-1","source_job_id":"img_original","source_sha256":"64-lowercase-hex-digits","prompt":"保留构图，细化花瓣"}
```

受理响应为 `{operation:"edit", request_id, request_digest, job}`。job 保持阶段 3 的公开结构，不包含服务路径或源图字节。lookup 沿用只读接口，找到时增加 `found:true`，operation 可为 submit 或 edit。

edit 摘要：对包含 operation=edit、source_job_id、source_sha256、prompt 的对象，以 Python `json.dumps(ensure_ascii=True, sort_keys=True, separators=(",", ":"))` 序列化后取 UTF-8 SHA256；request_id 不在摘要内。submit 沿用旧 SHA256(prompt UTF-8)，历史摘要不变。

同一签名归属的 submit/edit 共用 request_id 命名空间。事务中先查已存在请求：同操作同摘要返回原任务，跨操作或内容不同返回 `REQUEST_CONFLICT`。重放不会再次检查已失效原图或能力；只有新请求进行源图校验及排队。并发请求至多受理一个任务，lookup 从不补任务。

## 源图及输入

原图必须属于相同签名归属，状态为 completed 或 artifact_ready，且有已登记产物。不存在和跨归属均为 `SOURCE_NOT_FOUND`；未可用为 `SOURCE_NOT_READY`。图片路径必须在服务 state/artifacts 内，实际长度、SHA256、登记 MIME 一致，PNG/JPEG/WEBP 完整可解码，大小遵循服务上限、像素不超过 2500 万。非法图片为 `SOURCE_IMAGE_INVALID`，内容或元数据不符为 `SOURCE_HASH_MISMATCH`，路径越界为 `OUTPUT_PATH_REJECTED`。

新请求在 SQLite 事务内保存源任务、摘要、MIME 与字节快照；执行前重新读取原文件并与快照比较。删除或替换源图会让原编辑任务明确失败，不重新排队。Codex 驱动再验证快照，并以排他创建方式写入任务私有目录，turn/start 前复核文件字节。原产物不覆盖。

turn/start 的 input 同时包含编辑文字和 `{type:"localImage", path:受控绝对路径}`，不是把源图编号写进提示词。依据 [官方 App Server 文档](https://learn.chatgpt.com/docs/app-server#turns)，并使用本机 `codex-cli 0.166.0-alpha.5.1` 导出的 ClientRequest/UserInput schema 核实结构。只运行了本地 schema 生成，没有调用真实模型。`edit_v1` 表示驱动实现图片输入，不代表当前账号、模型已经通过真实编辑验收；驱动未声明 supports_edit 时拒绝，不提供文字重画降级。

## 状态、版本与迁移

reserved → submitting 只有一次转换可以发送 edit；后续 accepted、unknown 或终态不会重提。失去回执、取消、拒绝或协议不一致均保留原编号与未知占用，通过 lookup 对账；查不到不退款、不换编号补画。服务明确失败可按原已受理操作结算，已消费预算不退回。

collect 使用同一次 lookup 的产物元数据，再校验 result 图片，插件单版本上限 8 MiB。新版本绑定原 request/job 和父版本；重复 collect 返回同一版本。图片可用与回合完成分别处理：未知回合的图可保存，但未知占用继续阻止后续编辑。原图、历史评审和发送记录保持不变。

服务旧 jobs 表事务增加 operation（旧行默认 submit）、source_job_id/source_sha256/source_mime/source_bytes。迁移失败整体回滚，保留旧任务编号、摘要、产物、未知状态；不触碰真实库。新增 BLOB 每个 edit 保存一份受限大小的源图快照，当前没有自动清理。插件 schema 仍为 2，无新表迁移。

## 验证与边界

本轮以插件 `3d277fb`、独立服务 `52635ea` 为基线。测试使用临时 SQLite、合成图片、模拟 Codex 子进程和真实本机 MCP HTTP 传输。插件默认测试不需要独立服务；跨仓库测试显式设置 `CODEX_IMAGE_TEST_SERVICE_SOURCE` 指向其 src，使用具有两端测试依赖的解释器：

```sh
pytest tests/test_mcp_recovery_integration.py tests/test_mcp_edit_integration.py -q
```

联调覆盖正常编辑、受理后丢回执、重开账本、只读恢复、实际字节进入模拟 native IPC、子版本关联和重复收集。作者自测、主线回归及固定快照独立审查结果汇总于 [验证记录](validation.md)。

没有修改 MaiBot 本体、旧安装入口或真实配置，没有部署、调用真实绘图或发送。模拟输入证据不证明模型真正保留构图或绘图质量；真实编辑效果及用户验收尚未执行。独立服务暂未配置远端，与插件分别提交，不进入插件发行包。
