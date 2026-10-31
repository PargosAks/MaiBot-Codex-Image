# 阶段 6：Host 接入

插件 0.8.0 接入 MaiBot 的聊天事件、插件内定时唤醒、模型能力和图片发送能力。自主创作默认关闭，预算全部为零；旧配置补入这些默认值，不将命令白名单解释为持续创作委托。现有命令保持兼容。

本阶段只改插件。验证使用隔离数据目录、脚本模型、模拟绘图与发送；没有修改 MaiBot 本体、MCP、安装环境或运行配置，没有部署或进行真实绘图、模型调用、聊天发送。代码接线完成不等于真实效果验收。

## 模块与 Host 契约

- `plugin.py` 注册 `chat.receive.after_process` 的 `HookMode.OBSERVE` 观察器（当前主链已注释旧 `ON_MESSAGE` 入口），沿用 Host 顶层/嵌套身份一致性校验。只收集已委托空间中委托人的正常文本；命令、以 `/` 开头的文本、通知、缺失身份和其他成员消息不进入创作循环。观察点在轻量预处理后、主链关键词过滤之前，不能视为 Maisaka 最终接收确认。
- `host_runtime.py` 管理配置、持续委托、聊天流核验、空间 worker 和关闭。消息写入持久队列后唤醒 worker，事件回调不等待模型或绘图；无人时段进入同一个 Artist 状态机。定时器不意味着每次都调用模型或必画。
- `host_adapters.py` 通过 SDK 的原始 `cap.call` 调用 `llm.generate` / `send.image`，评审传入实际图片字节；发送只使用冻结的 Host 聊天流。原始回执避免 SDK 解包或布尔强转丢失成功标志。模型不得选择接收者、服务、身份或预算。
- `artist.py` / `store.py` 继续负责预算、未知占用、计划、版本、评审和分享。网络绘图和 Host 发送各需显式 opt-in；HostRuntime 只在已启用且存在有效委托时构造它们。

启动时通过 `chat.get_stream_by_user_id` 或 `chat.get_stream_by_group_id` 查找已有会话，核对平台、stream/session ID、用户或群 ID、群聊标识；不创建会话。群聊按委托人归属隔离，本阶段不自动采集全群成员的聊天。

开发 SDK 固定 2.16.0（`835e0e9`），另核对本机公开 SDK 2.17.0 源码；Host 公共接口依据 MaiBot `062d45cf8bd4272ac57d7ec03fcf405e5a458211`。测试具体范围见 [验证记录](validation.md)，不能将这些检查外推到整个 Host/SDK 声明范围。

## 配置与授权

`autonomy.enabled=false` 是总开关。启用前还需插件启用、有效 MCP 配对、Host 能力授权，以及明确的持续委托和额度。授权这些能力本身不会打开自主创作。模型规划任务为 `plan_task=utils`，图片评审任务为 `review_task=vlm`，对应 Host 已配置的任务名；评审模型必须支持实际图片输入。

`autonomy.spaces` 每个对象包含：

| 字段 | 含义 |
| --- | --- |
| `space_id` | 唯一、稳定的空间编号，不能复用到其他归属或 MCP |
| `platform`, `user_id` | 平台与持续委托人；必须在 `connection.allowed_users` 中 |
| `stream_id` | 真实 Host 已有会话 ID |
| `group_id` | 空值为委托人的私聊；群聊使用真实群 ID |
| `continuous` | 必须显式为 true；默认 false |
| `consent_ref` | 管理员录入的持续委托依据；不包含密钥 |
| `limits` | `think/render/share/max_inflight/max_revisions` 五项额度，默认零 |

全局预算使用 `autonomy.global_limits` 同样五项字段；全局和空间限额同时生效。`think/render/share` 按香港自然日计数，`max_inflight` 控制未决操作，`max_revisions` 限制作品修订。`idle_seconds` 默认 300、范围 60–86400；轮询 `tick_seconds` 默认 5、范围 0.1–60；外部操作超时默认 30 秒。persona 仅填写公共人格和画风。

关闭开关、移除空间或移除白名单委托人会停止旧 worker，并在账本撤销对应委托。相同配置重新加载保留原授权版本；修改委托依据会生成新的授权版本，旧评审不能跨版本自动应用。服务绑定由配置端点派生并冻结，改地址不能把已有空间的未知任务移到新服务；应先核查旧任务，再制定显式迁移。

## 生命周期与失败恢复

每个空间只有一个 worker。重载/卸载在等待命令持有的生命周期锁之前就停止旧 worker；随后等待 worker 与数据库线程结束，再关闭状态库和发送库。跨进程的 `artist.lock` 防止两个 runtime 同时恢复同一账本；锁文件不是可删除的重试开关。

启动只恢复本地领取状态，后续查询只读恢复原 MCP 请求，不自动补交未知绘图。共享 `delivery.sqlite3` 的发送意图锁同时约束命令和自主发送；发送回执丢失、取消或结构不明确保留未知状态，不盲目重发。当前 Host 发送层可能吞掉传输异常并仅返回 `sent=false`，因此 false 也保留未知；只有明确的 `E_CAPABILITY_DENIED` 能确认未进入发送处理器。模型失败回执同样保留未知调用占用，不能自动补调模型。命令和自主发送使用同一严格回执检查。关闭自主创作不删除状态、作品、授权审计、预算占用或旧发送记录。

需要新图和原图编辑时，独立 MCP 必须分别满足当前 Bridge 的 `lookup_v1` / `edit_v1` 契约。旧 MCP 的命令兼容性不代表支持自主编辑。缺少能力、模型无图像支持或 Host 拒绝权限时明确停在相应阶段，不用文字冒充看图、不退化重画。

测试和审查证据见 [validation.md](validation.md)。下一步真实环境验证须另行安排，保留现有登录与原服务状态；本阶段不会自动部署或启用。

## 隔离验证入口

默认运行根目录 pytest。可选通过 `CODEX_IMAGE_TEST_SDK_SOURCE` 指定仅供读取的公开 SDK site-packages，通过 `CODEX_IMAGE_TEST_HOST_SOURCE` 指定公开 MaiBot `src` 目录；不传时跳过依赖外部源码的检查。只加载/提取公共实现，不启动 MaiBot、不读取运行配置。跨 MCP 的合成图片联调仍使用 `CODEX_IMAGE_TEST_SERVICE_SOURCE`。详细范围和结果以验证记录为准。
