# 0.11.1 官方插件文档对照审查

日期：2026-11-05。基线为插件 `bb514a81f58764913d9d5ab69b490206dcbb1b43`（0.11.0），本轮工作树为 0.11.1。审查与修改仅涉及插件，独立 MCP 只做源码评估；不修改 MaiBot 本体，不部署，不调用真实模型、绘图或 QQ 发送。

## 依据及适用范围

对照 [MaiBot 插件开发指南](https://docs.maibot.chat/plugin/)及其导航子页，并以当前公开 Host/SDK 实现核对实际行为。文档站不同页更新日与示例版本可能不同；不将示例 SDK 区间直接写入插件清单，也不把声明支持范围当成逐版本实测。

| 对照项 | 结论与处理 |
| --- | --- |
| [入口与清单](https://docs.maibot.chat/plugin/manifest) | 根 `plugin.py`、`create_plugin()`、manifest v2 与插件 ID 保留；发行版本为0.11.1，依赖与能力声明未扩大 |
| [生命周期](https://docs.maibot.chat/plugin/lifecycle) | 三个回调完整；卸载等待任务及数据库清理，独立绘图服务不随插件重载启停 |
| [配置](https://docs.maibot.chat/plugin/config) | 配置模型和字段保持原样，`plugin.config_version`仍0.11.0；运行配置由Runner管理，不随发行打包 |
| [插件运行边界](https://docs.maibot.chat/develop/plugin) | 产品只依赖SDK和自己的模块，不导入Host的`src.*`；持久状态使用Host注入目录 |
| [Hook](https://docs.maibot.chat/plugin/hooks) | 保留原命名Hook与真实上下文身份；目录迁移不扩大消息来源、收集权限或模型可写字段 |
| [API参考](https://docs.maibot.chat/plugin/api-reference) | 实际Host调用对应现有五项能力声明；图片RPC超时和未知回执处理保留 |
| [WebUI页面](https://docs.maibot.chat/plugin/webui-pages) | 根声明保留，entry改为`assets/studio.html`；页面仍是内联单文件HTML，查询只读，使用`maibot`桥接 |
| [发布](https://docs.maibot.chat/plugin/submission) | 按完整文件集构建；显式白名单包含新子包及页面，不包含配置、凭据、数据库、日志和服务源码；没有提交插件市场 |

组件导航的11个子页均已核对：使用了[Tool](https://docs.maibot.chat/plugin/tools)、[Command](https://docs.maibot.chat/plugin/commands)、Hook、[API](https://docs.maibot.chat/plugin/api-components)及WebUI；其余[事件处理器](https://docs.maibot.chat/plugin/event-handlers)、[消息网关](https://docs.maibot.chat/plugin/message-gateway)、[LLMProvider](https://docs.maibot.chat/plugin/llmprovider)、[回复扩展](https://docs.maibot.chat/plugin/reply-extensions)、[首页卡片](https://docs.maibot.chat/plugin/home-cards)、[旧Action](https://docs.maibot.chat/plugin/actions)未使用，不因存在这些组件而强行增加声明。主线另浏览了[Vibe Coding](https://docs.maibot.chat/plugin/vibe-coding)，只作为开发流程参考。

## 修正与架构判断

**LAYOUT-01 / 已实现**：22个实现模块按职责移入六个子包，根保留Host入口；HTML移入静态资源目录。显式相对导入适应Host命名空间，不创建旧模块转发层。测试导入、SDK版本夹具的模块别名、页面路径、包清单及源码冻结范围同步更新。路径和升级方式见[架构说明](architecture.md)。

**DOC-01 / 已修正**：当前安装说明仍要求根`bridge.py`和`delivery.py`；改为完整子包安装。README补齐群念头与分享权限字段，说明委托人直接消息与其他成员话题启发的区别。维护环境中的旧内嵌service资料不作为当前开发入口；不清理用户已有测试、虚拟环境或历史数据目录。

**MCP-01 / 评估完成、未实施替换**：MCP不是MaiBot插件的强制依赖，可换成专用HTTP；独立服务负责的九项任务/素材操作、持久幂等、原图快照、只读恢复和Codex进程管理仍须保留。当前URL参与服务绑定，不能直接换地址或清库。建议保留独立服务，传输迁移另行实施，详见[方案比较](architecture.md#mcp-是否必须存在)。

**N-01 / P3、已修正**：独立审查指出`workspace/studio.py`模块说明仍列旧页面路径；全量测试后仅更新该说明中的两个路径，执行语句不变。静态对照单独核对这一说明变化，不冒称全量测试发生在它之后。

**V-01 / 条件性兼容风险**：官网WebUI桥接描述突发20次、持续每秒2次，固定Dashboard源码实际为200次及每秒20次。当前原图每块192KiB、最大8MiB，最多43次分块查询，符合已核对Dashboard的初始突发额度；采用官网较低限额的其他Dashboard可能停止页面。未在此类环境复现，也未在本轮改变图片传输或节流；部署到不同Dashboard前应单独验证大图查询。不能将当前结果扩展为所有1.3.x前端均兼容。

**V-02 / 文档与实现差异**：Hook文档使用messages描述模型消息，固定Host的replyer实际载荷为`items`/`item_schema_version`。插件按已核对Host实现处理，不机械改成`messages`，保留当前双SDK契约测试。

## 验证记录

- 迁移后现有测试收集：2711项，无导入错误。
- 首轮定向覆盖打包、实际公开Host的WebUI、双SDK聊天Hook、念头存储及群分享流程：394通过、10跳过；跳过项为Windows真实链接权限限制。
- 语义对照：基线的23个Python模块（含入口）与迁移后AST在反向归一化内部导入和N-01那条模块说明后相同；HTML按LF归一化相同。N-01在全量之后单独静态复核，执行语句没有变化。此检查用于确认目录迁移范围，不代替运行测试。
- 完整回归：**2694通过、20跳过、零失败**（208.83秒）。Python3.14.7、SDK2.16.0；显式接入公开Host1.3.15、SDK2.17、Dashboard、独立服务及适配器测试源码，外部效果为夹具。20项跳过均为Windows真实链接权限；33条JUnit `record_property`/xunit2格式警告属于报告元数据，不是产品运行警告。
- 新增发行包验证：真实Host1.3.15的Loader与SDK2.16.0，三个用例单独运行及全量均通过。使用独立解压目录和隔离子进程，验证任意安装名/插件ID、六个子包、组件及配置schema、嵌套HTML、模块清理及fresh classes，并验证缺模块/HTML拒绝。Runner注册使用公开方法AST和内存接收端，不是线上IPC验收。
- 62项发行白名单、包与仓库相对链接、版本一致性、指定Ruff规则及差异空白检查通过。manifest/pyproject/lock为0.11.1，配置版本0.11.0，未修改数据格式。
- 独立审查为**有范围PASS**：重新解析23个模块和64条相对导入，核对62项包清单、状态路径和Loader测试边界，未发现已确认的P0/P1/P2规范缺陷或迁移阻塞。N-01为说明修正，V-01/V-02保留版本差异。审查者未重跑完整回归；作者执行、独立静态复核及线上验收分别记录。

本轮不沿用0.11.0历史部署结果作为0.11.1的运行证据。真实群模型选择、绘图、QQ群收图及浏览器人工验收仍未执行；既有授权、记录及累计额度未访问或变更。

## 2026-11-06 部署验证

用户决定保留 MCP，授权先本地验证，再部署 VPS、测试并同步仓库。以下是新的部署证据，不改写上节 11 月 5 日仅本地审查的历史范围。运行代码与上一轮全量回归后的冻结文件一致。

| 编号 | 状态 | 实际验证与边界 |
| --- | --- | --- |
| LAYOUT-LOCAL | PASS | 已安装 MaiBot 1.3.15 / SDK 2.17.0 / Python 3.14.7，在独立实例通过真实 Runner IPC、配置回调、聊天事件、无人定时、模拟 MCP HTTP、模型评审及本地发送/WebSocket 链路；没有接入 QQ |
| LAYOUT-EFFECT | PASS | 聊天与无人各一幅作品，各一次生成、一次细化、一次分享；共 4 个合成输出、4 次图像评审、2 次本地送达。聊天参考图字节、编辑父版本及最终收图哈希均核对；不发送未被接受的原版 |
| LAYOUT-STUDIO | PASS_WITH_SKIPS | 本地两个私聊空间、嵌套 HTML、四项只读查询及 3 张已有合成图片校验通过。只读补跑未重新生成、评审或发送；无群导致的群记录项目显式跳过 |
| LAYOUT-GUARD | PASS | 独立审查固定升级脚本，13 项隔离保护测试通过：完整 SQLite/WAL 备份、原任务归属校验、记录/额度保留、卸载竞态拒绝、未知开关结果不重发、只回滚已知源码、恢复凭证与原配置绑定 |
| LAYOUT-VPS | PASS_WITH_SKIPS | VPS Python 3.13.16 / SDK 2.16.0，42 个测试文件：1886 passed、61 skipped、零失败（97.30 秒）。实际部署文件复制到临时目录后再测：197 passed、18 skipped、零失败（31.35 秒） |
| LAYOUT-PRESERVE | PASS | 备份后停用插件、安装 62 项白名单文件；22 个旧根模块和旧 HTML 按清单备份移出。停止及隔离测试期间全部账本行、schema、累计占用和消耗一致；不存在的群数据库没有被检查工具创建 |
| LAYOUT-LIVE | PASS_WITH_SKIPS | 恢复原生产配置后插件 0.11.1 加载成功；原配置字节、插件所有账本行、MCP 所有状态行与维护备份一致。MCP 0.6.0 health ready，MaiBot 容器与服务进程未重启。只读工作区校验一张已有图片（3,446,805 字节、18 块）及缩略图；5 项 GET 和4项查询的未鉴权访问被拒绝 |

VPS 首轮 61 项跳过：独立 MCP 测试源码未提供12项、固定 Host Git 历史未提供33项、缺 Node 的 DOM 测试6项、缺已安装 Host 包形式的 Loader 测试3项、Dashboard 测试源码未提供7项。第二轮18项为固定 Host 历史15项与已安装 Host Loader 3项。这些条件在本地完整回归中有单独覆盖，不能因此将 VPS 跳过写成通过。VPS 两条依赖弃用警告来自 websockets/uvicorn，不是测试失败。

### 失败、修正及复核历史

1. 本地夹具首轮包含 Windows 启动护栏、配置就绪竞态等问题；在本次新建隔离目录中修正，未修改产品、安装包或生产数据。旧轮中断后重新开启自主创作导致参考授权策略版本变化，现有计划在发起 Bridge 前按预期报 `REFERENCE_AUTH_CHANGED`。保留失败合成数据和证据，未覆盖冻结版本或绕过权限；改用新隔离实例一次授权两个场景，中途不撤销委托，完整链路通过。
2. 完整链路之后，画室断言把无群环境的 `PASS_WITH_SKIPS` 误当失败。原失败记录保留，只修正夹具对预期跳过及图片哈希状态常量的判断；自主保持关闭，复用已有合成图片完成只读检查，没有重放生成或发送。独立复核确认四个输出、四次评审和两次本地送达相互对应，自有进程已退出、测试端口释放。该只读阶段核对账本投影、图片字节和夹具审计，不宣称所有测试库文件逐字节相同。
3. VPS 安装结束时一次 Host 配置查询失败，脚本保留操作日志并停止，未重复安装或 toggle。随后只读检查确认源码已完整切换、插件仍停用；完整文件、配置、账本及服务身份复核通过后才继续测试。没有把最初失败的命令改记为成功。
4. 最初只读观察的一条旧 accepted 记录，在维护开始前由原生产流程自然变为 succeeded；再次查询原 job 确认归属、请求编号、完成状态及产物登记对应。维护以之后的完整一致性备份为基线，未主动恢复或重绘该任务。

本轮实际测试和部署的62项候选 ZIP SHA256：`fe1717325a0fe8eca607bf38ed4ce89a75229e8abbf1f49ffb62a113f8aa3451`。后续补入本段部署记录及 README 后仅文档字节变化，发布包摘要会变化；运行代码、页面、清单、依赖及测试内容保持验证时版本。备份、原始日志、凭据、账本和图片均留在私有维护位置，不进入仓库。

恢复的只是原先已授权的生产开关和限额；未新建授权、重置额度或清除未知发送锁。测试没有新调用真实模型、绘图或 QQ 发送，结束时恢复正常生产后的活动另计。没有执行浏览器人工验收或真实群模型/收图验收；这些限制继续保留。
