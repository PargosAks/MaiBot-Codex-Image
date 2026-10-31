# I5 参考图片输入协议

此迭代在独立 MCP 与插件之间增加 `reference_inputs_v1`，不改变旧 submit/edit 的参数或摘要。自主创作、原图编辑、评审和分享仍由同一个 Artist 推进。图片输入开关和媒体委托默认为空／关闭；无参考图的旧流程不依赖新能力。

## 可信输入与本地状态

每空间配置 `reference_inputs=false`、`media_consent_ref=""`。同时启用开关并提供单独的媒体委托依据后，仅接收同一委托人、同一真实聊天流的当前消息图片组件。只读取 Host 序列化的原始字节，不下载模型提供的链接、不解析任意路径、不扫描聊天历史。引用消息及近期消息暂不主动回溯，缺失原图时停止导入。

PNG、JPEG、WebP 每张最多 8 MiB、6400 万像素，拒绝动画；每条消息最多三张。插件与服务分别解码验证，导入不消耗绘图额度，也不调用视觉模型。外部素材记为 `authorized_chat`，摘要只是明确未评审的来源说明，不伪装成实际图片内容分析。

新计划的引用方式由可信配置确定并冻结为 `text_context` 或 `image_input`。旧文字计划不会因开关改变而变成图片输入；实际输入计划在能力缺失、撤权或关闭开关后不能降级纯文字绘图。外部素材不伪造作品、版本或绘图 job，来源与用途分别记录。

素材登记使用单独持久账本，绑定空间、服务、授权版本、原请求编号与字节摘要。状态为 reserved → submitting → succeeded；回执丢失保留 unknown，后续只查询原登记，查不到不再次上传。绘图在全部登记确认后才预留 render 预算；登记不会重置修订次数或改变发送状态。

## MCP 接口

所有操作沿用原签名封装，签名绑定 scope、operation、完整 args 和时效。工具名增加 `codex_image_asset_register`、`codex_image_asset_lookup`、`codex_image_render`，lookup 保留原名。

| 操作 | 严格参数 | 返回 |
| --- | --- | --- |
| asset_register | request_id、sha256、mime、image_base64 | asset_id、sha256、mime、size_bytes |
| asset_lookup | request_id | `{found:false}` 或 `{found:true,asset:{...登记回执}}` |
| render | request_id、mode、prompt、source、references | operation=render、request_id、request_digest、job |
| lookup | 原请求编号 | 原格式；新 render 请求的 operation 为 render |

mode 为 create 时 source 必须是 null；mode 为 edit 时 source 为 `{job_id,sha256}`，保持原作品父图身份。references 为一到三项有序 `{asset_id,sha256,role}`，role 限 character、style、composition、palette。原图加参考图总共不超过 32 MiB，输出仍为单图。实际 Codex 输入为原图在先、参考图按冻结顺序在后，带用途标签与原始图片字节。

health 只有在驱动实际支持时声明 `reference_inputs_v1`，并返回 `reference_limits={max_image_bytes:8388608,max_references:3,max_total_bytes:33554432,max_asset_bytes:67108864}`。插件取本地与服务限制的较小值；能力声明不代表已完成真实绘图验收。

## 幂等、恢复和错误

render 与 submit/edit 共用 owner/request_id 唯一域，跨操作复用编号返回 REQUEST_CONFLICT。先查原请求，再校验新素材，避免素材后续状态影响原任务恢复。素材登记使用独立唯一域，同编号、相同内容返回原回执；相同哈希不能跨归属取图。

render 摘要为下列对象经 JSON `sort_keys=True,ensure_ascii=True,separators=(",",":")` 编码后的 UTF-8 SHA256：

```json
{"operation":"render","mode":"create","prompt":"原始提示词","source":null,"references":[{"asset_id":"登记回执句柄","sha256":"原图摘要","role":"palette"}]}
```

本地仍用 action=submit/edit 统计额度，冻结 params 增加 `reference_contract=1`、`transport_operation=render`、`reference_inputs`。编辑另外保留 source_job_id/source_sha256。恢复校验远端 operation=render 和上述摘要；缺少新协议字段的旧记录继续原算法。

可复现测试向量：prompt=`rain`、mode=`create`、source=null，唯一参考为 asset_id=`ref_test`、role=`palette`、sha256 为 64 个 `1`，上述规范编码的请求摘要应为 `c4212e23dba8321a21a0665fbd74eddfe1484aea6376a425e5f29626ef12533f`。

主要错误为 REFERENCE_INPUTS_UNSUPPORTED、ASSET_NOT_FOUND、ASSET_HASH_MISMATCH、ASSET_INVALID、ASSET_QUOTA_EXCEEDED、ASSET_TOO_LARGE、REQUEST_CONFLICT。跨归属与不存在使用同一不存在响应。传输失败或错误回执不证明未写入；未知登记与绘图均只读恢复原编号，不自动重试、不释放未知预算。

原登记的 `asset_lookup` 不依赖当前绘图能力，也不重新上传；即使驱动停止声明参考图支持，仍可用原编号补回登记回执。新登记与新 render 继续检查能力、媒体策略和授权；只读恢复成功不授予继续创作的权限。

## 兼容与验收

| 组合 | 行为 |
| --- | --- |
| 新插件、旧 MCP、无 image_input 计划 | 旧 submit/edit/lookup 正常沿用 |
| 新插件、旧 MCP、image_input 计划 | 明确阻止，不降级纯文字 |
| 旧插件、新 MCP | 原接口与摘要兼容 |
| 新插件、新 MCP | 明确开关与委托后才允许新协议 |

兼容矩阵指客户端与服务协议，不表示旧服务程序可以读取新版 state。出现新 render 请求后不可把旧 MCP 二进制指向同一任务库，也不能通过恢复旧备份丢弃这些请求；保留完整状态，用兼容服务核查原编号。服务升级与回退由独立服务维护，插件重载不负责服务生命周期。

验证使用合成图片、真实本地 MCP 传输、fake Codex 与模拟发送，分别验证登记丢回执、绘图丢回执、重启、撤权、父图与辅助图顺序、字节哈希、修订上限和旧摘要。真实视觉质量、实际订阅绘图和真实收图仍需单独授权验收。
