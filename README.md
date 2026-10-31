# MaiBot-Codex-Image

MaiBot 绘图插件，通过独立部署的 MCP 提交任务、查询进度并发送图片。调用链为 **MaiBot 插件 → 独立 MCP → 宿主机官方 Codex CLI**。插件加载、重载和卸载不会启停 MCP。

插件 **0.9.0**，命令兼容 MCP **0.5.0**，自主创作需要具备 `lookup_v1` / `edit_v1` 的独立 MCP。本仓库仅维护插件，不再提供 MCP 服务源码、安装器或部署工具；原命令协议与发送库格式不变，自主创作默认关闭。已在本地 MaiBot 1.3.15 完成模拟模型／绘图条件下的真实 Host 链路与本地聊天收图验收；真实账号绘图、艺术效果及外部适配器未验收。具体范围见 [验证记录](docs/validation.md)。

## 升级前必读

**先检查旧 MCP 是否仍从本仓库 `service/` 路径运行，包括启动命令、工作目录及可编辑安装引用。若是，不可原地 `git pull` 或覆盖安装：0.7.0 会删除服务运行入口。** 旧进程暂时存活不代表下次加载或重启安全。

保留旧完整仓库供服务使用，将新版插件部署到独立目录。保持原 Host 插件身份 `pargosaks.maibot-codex-image`、原 `ctx.paths.data_dir`（含 `delivery.sqlite3`）、插件 config、secret 和服务完整 `state`。不要同时运行两个使用同一 `state` 的服务。步骤见 [安装与升级](docs/quickstart.md)。

## 安装与配置

1. 先有兼容 MCP 0.5.0，由服务维护者独立管理 Codex 安装、手动登录与服务运行。本仓库不提供新的独立 MCP 仓库地址。
2. 将插件源码或插件 zip 解压内容放入 MaiBot 实际加载的 `plugins/codex_image`，确保 `_manifest.json` 位于该目录根层。已有目录先按升级指南核查。
3. 在插件面板设置 `connection.url`、`connection.secret`、`connection.allowed_users`，授予用户 `codex_image_command` 命令权限及插件 `send.text` / `send.image` 能力，保存并重载插件。

要求 MaiBot 1.3.x、SDK 2.16.x / 2.17.x；声明范围不等于整个范围已实测。运行依赖由 MaiBot 插件依赖机制安装，开发命令见 [development.md](docs/development.md)。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.config_version` | `"0.9.0"` | 配置版本；保留已有配置 |
| `plugin.enabled` | `true` | 是否启用 |
| `connection.url` | `"http://127.0.0.1:8765/mcp"` | 从 MaiBot 运行环境可达的 MCP 地址 |
| `connection.secret` | `""` | 与服务一致，至少 32 字符 |
| `connection.allowed_users` | `[]` | 真实 `platform:user_id`，英文冒号；空列表拒绝所有用户 |
| `connection.poll_seconds` | `2` | 查询间隔，0.1–30 秒 |
| `connection.watch_seconds` | `360` | 自动等待时长，1–3600 秒；超时不取消服务任务 |

Docker bridge 容器内的 `localhost` / `127.0.0.1` 指向容器自身。连接宿主机 MCP 应使用实际可达的宿主机地址，并核对监听、防火墙与 MCP Host 校验；宿主机可达不代表容器可达。不要将宿主机 Codex 登录、服务密钥文件或 `state` 挂入插件目录。

## 使用

| 命令 | 功能 |
| --- | --- |
| `/画图 描述` | 提交一张新图并等待发送，会消耗账号额度 |
| `/画图状态 任务编号` | 查询原任务，不生成新图 |
| `/重发画图 任务编号` | 按发送记录规则发送已有图片，不重新生成 |
| `/画图诊断` | 检查 MCP 连接，不生成图片；不证明绘图登录或聊天收图成功 |

命令入口保留原有行为；自主创作可根据聊天或无人时段自行规划，并迭代已完成的原图，默认关闭。超时或结果未知时先查原任务；`sending/send_unknown` 自动和手动均禁止重发，不要清库绕过。

## 小画师开发阶段

后续迭代见 [小画师自主创作迭代计划](docs/artist-iteration-plan.md)：拆分实际图片对照评审、创作档案、参考素材、MCP 进度诊断和多参考绘图。具体实现与验证以各阶段记录为准，保持默认关闭。

I1 增加当前图、父图及旧最佳图的实际图片对照、冻结评审快照和自主分享认可检查；旧单图评审保留恢复路径。实现契约及迁移见 [I1 对照评审](docs/implementation-i1.md)，实际验证范围见 [验证记录](docs/validation.md)。

I2 增加空间私有的版本化创作档案与系列目标。计划冻结选定档案，生成、编辑和评审使用同一份快照；配置和边界见 [I2 创作档案](docs/implementation-i2.md)。

I3 增加经过认可的作品素材目录，素材选择与分享分开，见 [I3 素材与上下文](docs/implementation-i3.md)。I5 在单独的媒体授权下接收委托人当前消息图片，通过独立 MCP 的 `reference_inputs_v1` 登记和输入实际参考图；默认关闭，不读取历史聊天或模型提供的图片链接，见 [I5 接口与兼容](docs/implementation-i5.md)。

阶段 6 将已有状态机接入 Host 的真实聊天 Hook、定时唤醒、模型图片评审和发送接口。`autonomy.enabled=false`、空间持续委托默认关闭、全局及空间预算默认零；启用还需核验已有 Host 会话与白名单。模型独立决定画什么、是否细化、是否分享。验证使用模拟绘图、脚本评审和发送，不代表真实效果验收。配置、生命周期和边界见 [阶段 6](docs/implementation-stage-6.md)。

[环境核查](docs/artist-stage-0.md) · [阶段 1 实现与验证](docs/implementation-stage-1.md)。开发阶段不扩大已声明的 SDK 兼容范围。

[阶段 2 运行方式与验证](docs/implementation-stage-2.md)。阶段 3 已增加 Bridge 能力探测、错误码透传和原请求查询恢复，独立 MCP 源码增加 `lookup_v1`，尚未部署或自动启用；旧服务缺少能力声明时不会盲猜支持。详见 [阶段 3](docs/implementation-stage-3.md)。

阶段 4 增加显式原图编辑组件、源图校验、跨 submit/edit 幂等和不可变父子版本关联。独立 MCP 的 `edit_v1` 使用实际图片输入；已用模拟 Codex 验证，尚未验证真实账号编辑效果。详见 [阶段 4](docs/implementation-stage-4.md)。

阶段 5 复用同一创作状态机串联新图、原图编辑、按图片字节评审、最佳版本选择和独立分享决策；丢回执或重启只恢复原操作，未知发送不重发。验证使用模拟绘图、评审和发送，以及隔离的真实本机 MCP 传输，不代表真实模型效果或聊天收图通过。运行与恢复入口见 [阶段 5](docs/implementation-stage-5.md)。

[安装与升级](docs/quickstart.md) · [配置与迁移](docs/advanced.md) · [排错](docs/troubleshooting.md) · [开发](docs/development.md) · [验证](docs/validation.md)

旧服务与部署资料仅供原部署维护参考：[固定提交 e50a7b6 的完整仓库](https://github.com/PargosAks/MaiBot-Codex-Image/tree/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272)。历史说明不是 0.7.0 安装入口。
