# I1 实际图片对照评审

在阶段 6 的 Host 链路上增加多图评审契约。当前图、实际父图和旧最佳图同时进入评审；重复版本合并角色，最多三张。自主创作仍默认关闭，未知绘图与发送不重放，不改变命令发送接口或独立 MCP。

## 契约与模块

- `Artist._review` 从当前版本的 `parent_id` 找实际父图，从作品的 `best_version` 找旧最佳图。每张图读取原始字节并核对 SHA256，不能用历史文字描述代替图片。
- `ArtistStore.freeze_review_snapshot` 持久化契约 2 的完整上下文、图片标识／角色／SHA256／MIME、授权版本和输入摘要。快照以空间及当前版本唯一，不因唤醒、档案或 best 变化重新拼装。
- `Thinking.review(..., images=...)` 校验图片与快照逐项、有序一致。每图上限 8 MiB，总量上限 24 MiB，支持 PNG、JPEG、WebP；这些是插件边界，不证明所配视觉模型接受同等大小。
- `HostThinking` 将每张图的版本及角色标签与实际 `image_url` 内容块送入 Host 的 `llm.generate`。一次评审仍计一次 think 调用；图片数和字节上限独立限制输入，不能把次数预算解释为固定 token 或账单。

契约 2 输出保留 `decision/reason/strengths/issues`，并要求：

```json
{
  "comparison": {
    "improvements": [],
    "regressions": ["背景调整后主体轮廓减弱"],
    "preserved": ["保留暖光配色"],
    "evidence_version_ids": ["version-current", "version-parent"]
  }
}
```

证据只能引用本次实际输入版本，且至少一项。`best_version_id` 同样仅可选择实际输入版本；当前图退步时可接受旧版本。`revise` 仍须给出修改目标及保留内容，并继续从当前图修订；本阶段不开放任意历史分支。

## 恢复与迁移

Artist schema 从 2 事务升级到 3，新增 `review_snapshots`，保留原操作、预算、版本、评审及发送记录；schema 1 按顺序升级。旧程序会拒绝新 schema，不能只降低 user_version 来回退。

新评审请求身份绑定契约、空间、作品、当前图及输入摘要。已知失败按既有退避尝试；未知 think 先阻断后续调用，不能借新快照或契约绕过。缓存只复用匹配契约和快照的成功结果，并检查当前授权版本。

旧 reserved 评审继续使用原单图参数；旧成功结果按契约 1 恢复，不被称为多图评审。旧未知调用保持原编号和占用。新成功评审即使尚未写入 reviews 就崩溃，也从原 think 结果补记，不再次调用模型。

## 认可与分享

`finished` 和 `best_version` 不再足以授权自主分享。`version_accepted` 核对成功的原评审操作、明确 accept、选定版本、真实输入、输入摘要和授权。契约 1 仅证明模型看过当次 current，不能因为它从目录选了旧图，就认定旧图被看过。

修订预算用尽可以停止绘制，但不会给作品补造认可；无有效认可的作品保留原图并阻止新的自主分享。旧命令的发送规则不变。

原图可用而绘图回合未知时允许评审，但已完成作品仍由 `run_once` 优先对账，不能进入新规划、新作品或自主分享。得到确定结果后再恢复决策；未知发送始终受原锁约束。

## 验证与运行边界

测试使用合成 PNG、脚本评审、模拟绘图与发送。重点覆盖三张不同版本的实际字节、角色合并、保留旧最佳、损坏父图、预算耗尽不自动认可、旧 reserved 恢复、未知调用与崩溃恢复、撤权、schema 迁移及 Host 内容块。

新增入口为 `tests/test_review_contract.py`、`tests/test_review_store.py`、`tests/test_review_flow.py`；同时回归现有 Artist 和 Host 流程。实际测试和审查结论见 [验证记录](validation.md)。模拟比较不能证明真实视觉判断质量，真实模型能力、编辑效果、聊天收图和用户验收仍需单独验证。

后续 I2～I5 见 [迭代计划](artist-iteration-plan.md)。
