# 配置与迁移

## 插件与服务边界

插件 **0.11.0** 的手动命令兼容独立 MCP **0.5.0**；自主创作另需 `lookup_v1` / `edit_v1`。插件提交、查询和取图发送，服务负责 Codex、登录、队列、任务、产物与 `state`。升级时保留原空间与数据目录，并按[工作区迁移说明](studio.md#创作档案)核对旧档案和预算。

I5 参考图片功能由独立 MCP **0.6.0** 提供，以 health 协商的 `reference_inputs_v1` 与 `reference_limits` 为准，不仅检查版本号；缺少能力时阻止依赖操作，不降级纯文字绘图。协议兼容不表示旧服务可读取新版 state，具体见 [I5 接口与兼容](implementation-i5.md)。

配置包含 `plugin.enabled/config_version`、`connection.url/secret/allowed_users/poll_seconds/watch_seconds` 和默认关闭的 `autonomy`，不增加 `service.mode` 或插件 `codex.*`。白名单由插件面板维护，不要求服务另建名单；连接鉴权与任务归属校验继续生效。

## 配置约束

- URL 允许 HTTP / HTTPS，不能带用户名、密码、查询参数或 fragment；使用实际 MCP 入口，不将 secret 放入 URL。
- secret 至少 32 字符且与服务一致。Bridge 使用 Bearer 和签名，不跟随重定向，也不继承环境代理；部署方须提供直接可达的入口。
- allowed_users 使用真实 `platform:user_id`，空列表拒绝所有人；Host 命令及 `send.text` / `send.image` 能力另行授权。
- poll_seconds 默认 2 秒，范围 0.1–30；watch_seconds 默认 360 秒，范围 1–3600。等待结束只停止自动取图，不取消或重提服务任务。

Docker bridge 的 localhost 属于容器自身。核对实际 URL、监听、路由、防火墙及 MCP Host 校验，只保留必要且受保护的访问链路。服务凭据和任务目录不进入插件挂载。

## 从旧版本迁移

升级前核对旧 MCP 启动命令、工作目录、解释器及可编辑安装源码引用。**若仍引用本仓库 `service/`，不得在旧目录原地 `git pull` 或覆盖安装 0.7.0。** 删除源码可能破坏后续加载及重启，进程尚存不能证明安全。

保留旧完整仓库给服务，插件部署到独立目录。切换加载位置时保留 Host 插件身份 `pargosaks.maibot-codex-image` 和原 `ctx.paths.data_dir`，避免重复加载同一插件；不要移动服务仍引用的旧目录。

| 必须保留 | 原因 |
| --- | --- |
| Host 身份、原 `data_dir`、`delivery.sqlite3` | 发送意图、结果及未知发送保护 |
| 插件 config、名单、启用状态和权限 | 保留连接及授权 |
| 原 URL、secret、服务配置与 Codex 登录目录 | 继续访问原服务，不自动换密钥或复制凭据 |
| 完整服务 `state` 及产物 | 保留任务、归属与幂等，不只搬图片或单个数据库 |
| 旧完整仓库及原运行环境 | 保留服务入口与恢复方式 |

同一 `state` 只运行一个服务实例。插件升级无需启动第二个 MCP；确需服务迁移时，另行安排停机及一致性备份，确认原实例停止并释放目录后才可启动替代实例。不删锁、清库或重建密钥绕过冲突。

旧 0.2 / 0.3 内置任务不因填写新 URL 自动迁移。保留旧数据与发送记录，由维护者确认兼容及迁移方案，不宣称自动导入或安全重放。

具体插件步骤见 [quickstart.md](quickstart.md)。旧服务维护参考 [固定提交 service](https://github.com/PargosAks/MaiBot-Codex-Image/tree/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272/service) 与 [历史部署指南](https://github.com/PargosAks/MaiBot-Codex-Image/blob/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272/docs/host-service.md)，不作为当前可执行安装步骤。

## 发送记录与未知结果

发送前写入 `sending`，加载时遗留 `sending` 转为 `send_unknown`，两者均不能自动或手动重发。`sent/send_failed` 仅在手动路径按原规则发送原图，不创建新生成。

绘图超时、`outcome_unknown` 或发送未知时，先查原任务与实际聊天记录，由维护者核对；不删发送库或换任务编号绕过保护。健康检查、任务完成、发送确认与聊天收图分别验证。

自主创作默认关闭；如需了解持续委托、预算、Host 能力、模型任务和失败恢复，见 [阶段 6](implementation-stage-6.md)。MCP 0.5.0 的命令兼容性不等于具备自主创作所需的 `lookup_v1` / `edit_v1`。
## 群创作念头的开关

在既有群空间的 `autonomy.spaces[]` 中，`group_inspiration = true` 表示允许 MaiBot 在该群已有规划回合自主记下视觉念头，默认是 `false`。它不要求把群成员加入 `connection.allowed_users`；白名单和持续委托仍约束原委托人。空间的真实群、平台和会话必须匹配且归属唯一。

第二阶段会将念头导入同一空间的自主规划，记录后通知原有 worker；是否采用、绘图及分享仍是独立决策，不能凭记录本身越过授权和额度。聊天与无人唤醒共用这一路径。关闭 `group_inspiration` 会停止记录和新采用，保留既有作品计划及采用历史；停止整个创作流程仍使用 `autonomy.enabled` 或撤销持续委托。不要为查看念头重置额度或启用绘图。操作说明见[群灵感簿](group-inspiration.md)。

第三阶段在同一 `autonomy.spaces[]` 下增加独立字段：

| 字段 | 取值与效果 |
| --- | --- |
| `group_inspiration` | 默认 `false`；开启后允许记录群念头并供自主规划采用 |
| `group_share_mode` | 默认 `"collect_only"`，自主作品仅收藏；`"original_group"` 允许模型自主决定分享到 Host 核验的原群 |

“仅收藏”不关闭创作和评审；这些操作仍可消耗原思考、绘图额度，产物可在工作区查看。分享模式约束整个群空间的自主作品，不能通过不关联念头绕过。关闭收集开关不撤销既有作品的分享许可；禁止后续自主分享应改为 `collect_only`，停止全部自主工作仍使用总开关。私聊与用户显式命令沿用各自既有授权，群成员不会因此得到命令权限。

旧群配置缺少 `group_share_mode` 时按仅收藏处理，不将历史 share 额度视为新授权。启用 `original_group` 后也不会立即批量发送，模型仍可等待或只收藏。已提交发送可能已经到达 QQ，关闭权限不能撤回；`sending/send_unknown` 保留原锁，不因重新开启而重试。

首次打开创作账本会在事务中从 schema 1–8 升级到 9，新增群分享策略与预留时权限版本，保留原任务、授权、累计用量及未知发送记录；独立念头库仍为 schema 1。旧的群分享预留若缺少权限版本，会保持记录和额度但拒绝自动派发；不删记录或改请求号绕过。旧插件不支持 schema 9，不能直接降级读取，需保留升级前备份并先处理在途任务。工作区查询自身不执行迁移。
