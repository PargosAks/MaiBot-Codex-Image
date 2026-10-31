# 小画师阶段 5：统一流程与 MCP 对接

阶段 5 将阶段 2 的 Artist 状态机连接到阶段 3/4 的 MCP 查询、新图和原图编辑。创作意图仍由注入的 Thinking 决定；聊天和无人时段只是唤醒来源，没有新增创建绘图的命令。阶段验证使用脚本模型、模拟绘图驱动和模拟发送器，真实本机 MCP HTTP 负责两端协议传输。

## 范围与职责

| 模块 | 本轮职责 |
| --- | --- |
| artist.py | 统一唤醒、计划、操作预留、绘图推进、视觉评审、版本记录和独立分享；唯一账本写入协调者 |
| rendering.py / MCPRenderer | 将既有 Renderer 协议转换为 Bridge 调用；核对空间/归属/服务绑定/冻结参数，读取原任务及图片 |
| store.py | 增加按当前空间和 job_id 找回唯一绘图操作、提交前复核的只读查询；schema 保持 2 |
| thinking.py | 注入结构化计划和图片评审回调，返回建议而非授权；本轮无实际模型调用 |
| bridge.py / recovery.py / editing.py | 保留协议验证和显式恢复/编辑接口；能力探测后可执行可信提交检查，独立 ImageEdits 使用同样规则，不叠加到 Artist 的预留流程 |

`MCPRenderer(store, space_id, scope, bridge, binding_id=...)` 的 Scope 和 binding_id 必须来自可信调用方，Bridge 必须对应原空间绑定的服务。binding_id 是配置身份校验，不是远端服务器证书；调用方不得把旧编号配给另一个地址或密钥。模型上下文不包含这些信息，也不能选择发送目标、任务编号或文件路径。

适配层只读取本地操作，不重复预留、结算、记版本或发送。Artist 继续使用既有请求编号及单空间领取机制；新图和编辑操作各自只提交一次。阶段 4 的 `ImageEdits` 仍可单独显式使用，不能与 Artist 同时推进同一作品。

## 创作流程

1. `on_chat(message_id=..., text=...)` 或 `tick()` 保存/领取唤醒。消息重复不新增事件，多个运行器由 SQLite 领取权串行推进；无人时段不补跑离线期间每个定时点。
2. Thinking 决定积累灵感、等待、创建计划、继续、延期或放弃。已有作品进行时，新聊天只积累后续灵感，不覆盖当前意图。
3. Artist 复核授权与预算，冻结 request_id、参数和原图版本；提交前持久化 submitting。MCPRenderer 核对这些记录，再使用 MCP submit 或 edit，不把编辑降级成文字新图。
4. 适配层通过 lookup 校验操作、摘要和 job_id，再按产物 MIME、长度、SHA256 读取图片。已知 job_id 也从本地账本反查原请求；重开进程不丢映射。
5. Artist 保存不可变版本，把真实取回的图片字节和 MIME 交给 Thinking.review。评审可接受、细化或放弃，也可以选择本作品旧版本为最佳版本。修订必须含改进目标及保留内容；累计修订预算和原图关联继续生效。
6. 完成不自动分享。后续规划独立决定 share，发送前保存意图，只发送绑定空间选定的版本。发送结果未知、取消或重启都不自动重发；原有 legacy 发送锁仍保留。

能力探测会等待网络，不能沿用探测前的授权和额度。新图在探测后重新校验提交记录；编辑通过 `Bridge.edit(..., before_dispatch=...)` 在其自身探测后再复核授权版本、全局/空间预算、并发及修订上限。独立 ImageEdits 也使用该检查。若已跨原记账日，返回 `BUDGET_DAY_CHANGED`，保留原操作而不改写日期、自动退款或重提。这里不持有数据库事务等待网络，也不声称本地校验与远端受理是原子事务；已开始的提交仍按在途操作恢复。

MCP 的产物可用与回合结果分开映射。queued/starting 为待执行，running 为运行中；artifact_ready 还要看 turn_status。图片可供评审时仍可能保留未知额度，未确认原回合结束前不能开始下一次细化。已受理失败保留远端规范错误码；通信、协议、摘要或图片校验失败不被解释为“质量差”，只能取回原任务，不新建补画。

## 显式运行入口

`Artist` 默认继续要求模拟绘图。网络适配器声明 `simulation=False`，必须显式传入 `allow_network_renderer=True`；这只是调用方选择网络后端，不验证远端驱动是否模拟，也不构成生产运行授权。Sender 在此阶段仍必须声明 `simulation=True`。

隔离测试中的组装形式如下；其中 bridge 只连接测试 fixture，model/sender 均为测试对象：

```python
renderer = MCPRenderer(store, space_id, trusted_scope, bridge, binding_id=trusted_binding)
artist = Artist(store, space_id, trusted_scope, Thinking(model), renderer, sender,
                allow_network_renderer=True)
await artist.on_chat(message_id=trusted_message_id, text=message_text)
await artist.run_once()
await artist.tick()  # 有无聊天都进入同一持久流程
```

网络绘图开关不增加用户配置字段，也不自动读取生产连接、建立授权或启动任务。`plugin.py` 未挂接 Host 生命周期、真实消息监听或定时器；下一阶段需另行完成可信 Host 事件/模型/发送适配及限定空间验收，不应仅打开开关就视为上线。

## 故障恢复

在旧推进者已经停止后，`await artist.recover(offline=True)` 将在途状态转为未知并释放旧领取记录，不调用网络。可显式使用 `await artist.recover(offline=True, reconcile=True)` 读取各作品原 pending 请求并保存结果，返回逐项状态；它不思考、不提交、不分享，也不确认未处理的聊天唤醒。撤权后仍允许此原结果恢复。恢复后继续创作需要有效的新操作授权，`offline=True` 不能在另一个活跃推进者还在写入时使用。

- 受理后丢回执：保存未知，lookup 找原编号；found=false、冲突或能力缺失都不换编号重提、不自动退款。
- 取图或评审失败：保留原作品和任务，只重取原图或在持久退避后重做已知失败的评审。未知模型调用不能靠新聊天、定时或跨日获得新的调用编号绕过占用。
- 评审已完成但记录中断：从原成功思考操作复用评审结果，核对授权版本后补记；不重复看图调用。已经落库的评审在应用前也核对原思考操作及当前授权版本，撤权后重授不能直接沿用旧建议推进修订；原记录保留，不自动覆盖。
- 发送回执丢失：保持 share unknown，占用及防重发状态跨重启保留；MCP lookup 不能证明聊天已经送达。
- 撤权：阻止新的思考、生成、编辑和分享，不删除在途任务或图片。已提交操作不被撤权伪装成未执行。
- 重复取消：数据库线程结束后才释放当前循环；不因取消 await 误以为写入线程已经停止。

## 验证入口与实际边界

插件基线 `08c06ab`，配套独立 MCP 固定 `9abea67`；本轮不修改服务。默认根测试自包含，跨仓库用例需显式设置 `CODEX_IMAGE_TEST_SERVICE_SOURCE` 为独立服务 src，并使用具有两端测试依赖的解释器：

```sh
pytest tests/test_artist_mcp_integration.py tests/test_mcp_edit_integration.py tests/test_mcp_recovery_integration.py -q
```

完整测试与独立审查结果见 [验证记录](validation.md)。测试数据库、图片、Scope、授权和服务密钥均为合成数据；没有访问真实登录、运行状态或聊天系统。插件包仍不包含 MCP 服务、测试环境或私密数据，无新增运行依赖、配置版本或数据库迁移。

没有修改 MaiBot 本体、已安装包或独立 MCP，没有部署、调用真实模型、绘图或发送。模拟评审仅证明图片字节和决策流正确传递，不证明视觉判断质量；真实创作效果、编辑效果、Host 接线、聊天收图与用户验收尚待后续阶段。
