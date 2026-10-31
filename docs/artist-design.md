# 小画师设计与实施契约

插件负责创作决策、状态与分享，独立 MCP 负责一次生成或原图编辑。聊天灵感、无人时段唤醒和任务完成进入同一流程。采用插件内部模型决策循环，不修改 MaiBot 原生 Planner；MCP 部署及生命周期仍独立维护。

本文保留阶段 0/1 时的设计基线。阶段 6 的实际 Host 接入见 [阶段 6](implementation-stage-6.md)；在现有能力上扩展对照评审、创作档案和素材管理的拆分与技术方案见 [后续迭代计划](artist-iteration-plan.md)。历史阶段表不表示真实效果已经验收。

本设计中的自主创作、编辑、查询接口按阶段实现，不能视为当前 0.7.0 已提供的功能。阶段 0/1 只交付环境核查和离线状态底座，不启用自主任务、不迁移真实数据库、不修改 MCP 或 MaiBot、不部署、不调用真实绘图或发送。

## 模块与职责

| 模块 | 职责与边界 |
| --- | --- |
| plugin.py | Host 事件、可信授权来源、配置与生命周期；事件只提交唤醒 |
| artist.py | 唯一流程推进器，合并唤醒，安排规划、绘图、对账、评审与分享 |
| thinking.py | 使用 MaiBot 模型提炼灵感、计划、看图评审；只返回结构化建议，无外部副作用 |
| policy.py | 授权、空间隔离、预算、修订及发布限制；模型不能提供可信身份 |
| store.py | SQLite 事务、持久状态、原子额度预留、迁移及领取权 |
| bridge.py | MCP 签名、协议、错误码和图片解析；不决定重试生成 |
| delivery.py | 分享意图、目标、发送结果与防重发；不能隐式生成 |

先不引入消息总线、代理框架或服务部署依赖。未来自主模式不注册创建绘图的命令；诊断与已有任务查询不产生新任务。阶段 0/1 不改变原有命令运行路径。

## 创作空间和数据

每个空间固定真实的 platform、stream_id、user_id 和持续授权来源，仅分享到来源聊天。不同空间可以共用公开人格和画风，但不能共享私有灵感、作品或授权。历史授权消息与本次触发分别记录；无人任务的本次 message_id 可以为空。曾发言或进入白名单不等于持续委托。更换 owner 不接管旧任务。

持久记录包括：空间和授权；灵感及有效期；作品计划、完成标准、下次检查时间；不可变作品版本和父版本；评审；冻结参数的操作记录与额度预留；版本和目标绑定的分享记录。尚未出图的作品就是计划，不另建项目管理系统。

## 状态与不变量

作品主状态：planned → creating → review_pending → finished。planned 可以转 deferred，待恢复条件满足后再规划；不再创作时转 abandoned。finished 再次修订仍沿用原 work_id 和累计上限。阶段 1 只实现后续流程需要的持久底座，不执行此状态机。

受理状态、MCP 执行状态、产物可用性、评审和分享分别保存。有图不代表绘图回合已结束；操作 outcome_unknown 与图片 available 可以共存。未知操作保留预算与并发占用，不自动开启下一轮修订。

- 每次修订创建新版本，旧图及旧评审不覆盖，最佳版本可以是旧版本。
- 修改必须指明目标、保留内容和改善预期；达到上限不靠新建同义计划规避。
- 看图接口失败或产物损坏不等于质量差；先取原产物或恢复评审，不重画。
- finished 不要求分享。分享是独立决策，目标来自真实空间，不来自模型自由文本。
- 放弃作品只停止后续，不能取消或抹去已经在途的操作。
- 撤权阻止新的决策、提交与分享，保留原记录供只读对账。

分享状态：ready → sending → sent / send_failed / send_unknown。重启发现 sending 时按未知处理，自动和人工均不得绕过未知锁。旧 delivery.sqlite3 只有 job 和 state；目标无法还原时保留 job 级 legacy 锁，不能猜测目标或视为未发送。

## 事务和恢复

外部操作前，在同一事务内复核最新授权和预算，保存稳定 request_id、完整参数、归属及额度预留。提交事务后才调用外部服务，收到明确结果再落库。超时、取消或重启只对账，不重放生成或发送。

同一空间只有一个推进者；新唤醒不能被旧一轮完成操作清除。重启只合并为一次当前检查，不补跑每个错过的定时点。全局和空间预算原子预留，配置收紧即时约束旧计划；未知预留跨日仍占用，不靠日期滚动解锁。已受理失败按尝试消耗，不假定没有账单。

阶段 1 的具体 API、预算记账与迁移约束见 implementation-stage-1.md；运行接入和自动领取属于后续阶段。

## MCP 新接口契约

以下为独立 MCP 的后续变更，本插件阶段 0/1 不实现或调用。保留 scope/op/args/iat HMAC、真实 owner、原四接口与时效校验；稳定 request_id 不变，每次调用重新签名。

### 原图编辑

工具 codex_image_edit，op=edit，args 严格为：

```json
{"request_id":"stable-operation-id","source_job_id":"img_source","source_sha256":"64-lowercase-hex","prompt":"修改要求及保留内容"}
```

request_id 1～200 字符，prompt 1～4000 字符且非空白，source_sha256 为 64 位小写十六进制，取自原任务 status.artifacts。源任务必须同 owner、有已登记可用产物，状态为 completed 或 artifact_ready。服务读取受控产物并核对实际字节、大小、类型和哈希；固定此次源图，执行前复核，真正向绘图后端传入这些图片字节。不接受任意路径或外部网址，不支持原图输入时不能降级成文字重画。输出是独立的新任务与产物，不覆盖源图。

### 只读查询

工具 codex_image_lookup，op=lookup，args={"request_id":"stable-operation-id"}。仅查当前 owner。

```json
{"found":true,"operation":"edit","request_id":"stable-operation-id","request_digest":"sha256","job":{"job_id":"img_new","status":"queued","turn_status":null,"error_code":null,"artifacts":[]}}
```

未找到返回 {"found":false}。查询不能入队、补任务、生成或推进状态。found=false 仅为当前 state 快照，不证明原请求未执行；原请求仍在途、state 更换、回滚或记录丢失均可能导致未找到，不自动授权重提或退款。找到后比对操作和摘要，不匹配不得绑定。edit 受理成功返回同样的请求和 job 字段，但不带 found；旧接口响应保持兼容。

### 幂等和兼容

submit/edit 共用 UNIQUE(owner, request_id)。先比较操作类型，再比较内容摘要；同键同内容返回原任务当前快照，即使已失败或未知，也不新增任务。不一致返回 REQUEST_CONFLICT。重放先查重，不因源图后来失效而丢失原任务；查重和插入必须处理并发。

旧 submit 摘要保留 SHA256(prompt UTF-8)，旧行的 operation 按 submit 解释。edit 摘要覆盖 operation=edit、source_job_id、source_sha256 和原始 prompt；JSON 使用 sort_keys=true、ensure_ascii=true、分隔符逗号和冒号且无空格，然后 UTF-8 编码计算 SHA256。不自动裁剪或改写参与摘要的字符串。

MCP 错误保留 isError 和机器可读 error.code：INVALID_ARGUMENTS、EDIT_UNSUPPORTED、SOURCE_NOT_FOUND（跨归属与不存在统一）、SOURCE_NOT_READY、SOURCE_HASH_MISMATCH、REQUEST_CONFLICT、QUEUE_FULL。受理后的源图变化记录在原 job，不删除幂等绑定。超时、断线、响应无法解释是未知，不等同确定拒绝。

本次拒绝不证明同编号此前从未受理；REQUEST_CONFLICT 不退款，先 lookup 对账。Bridge 必须保留分类，不能把所有错误压成同一种可重试异常。

health 增加可选 capabilities=["edit_v1","lookup_v1"]。缺失按旧服务处理，不猜测支持，不静默降级编辑。新增工具不要求旧插件升级；源码支持不等于实际绘图后端已通过验收。

## 分阶段实施和验收

| 阶段 | 范围 | 验收门槛 |
| --- | --- | --- |
| 0 | 固定实际版本、环境证据、服务维护入口和协议 | 区分源码、安装与运行事实；无法核实项明确阻塞边界 |
| 1 | store/policy、授权空间、预算、操作编号、旧发送库迁移 | 并发不超额；重启未知锁保留；跨空间拒绝；旧库完整保留 |
| 2 | artist/thinking、统一唤醒、灵感计划及评审，模拟后端 | 新唤醒不丢；可等待、放弃、不分享；评审失败不重画 |
| 3 | 独立 MCP lookup、能力声明、错误码，Bridge 对接 | 丢响应可找回；not_found 不重提；冲突不退款；旧接口兼容 |
| 4 | 独立 MCP edit、源图与幂等、版本关联 | 原图实际输入；同请求一次；跨归属拒绝；源图不覆盖 |
| 5 | 分享与完整故障恢复 | 丢回执不重发；重启不重复绘图；撤权阻止新操作 |
| 6 | 限定空间和额度的真实验收 | 分别验证生成、视觉评审、原图编辑及聊天收图 |

前五阶段采用隔离测试数据，真实验收需另行确定绘图额度与发送范围。设计完成、离线测试通过、部署、真实链路和用户验收分别记录。
