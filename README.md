# MaiBot-Codex-Image

MaiBot 绘图插件，通过独立部署的 MCP 提交任务、查询进度并发送图片。调用链为 **MaiBot 插件 → 独立 MCP → 宿主机官方 Codex CLI**。插件加载、重载和卸载不会启停 MCP。

插件 **0.7.0**，兼容 MCP **0.5.0**。本仓库仅维护插件，不再提供 MCP 服务源码、安装器或部署工具；协议、配置字段与发送库格式不变。已通过本地插件回归及旧 MCP 模拟 Codex 兼容检查；尚未部署或进行本版真实聊天收图验收，见 [验证记录](docs/validation.md)。

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
| `plugin.config_version` | `"0.7.0"` | 配置版本；保留已有配置 |
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

仅支持命令触发，不支持改图、参考图或批量生成。超时或结果未知时先查原任务；`sending/send_unknown` 自动和手动均禁止重发，不要清库绕过。

## 小画师开发阶段

自主创作采用插件内部决策循环，设计见 [小画师设计](docs/artist-design.md)。当前开发只增加阶段 0/1 的环境核查和离线状态、授权、预算及迁移底座；尚未接入插件生命周期，不会自动迁移已有数据、绘图或发送，现有命令行为保持不变。

[环境核查](docs/artist-stage-0.md) · [阶段 1 实现与验证](docs/implementation-stage-1.md)。MCP 编辑与只读查询仍是后续接口契约，当前没有实现，也不扩大已声明的 SDK 兼容范围。

[安装与升级](docs/quickstart.md) · [配置与迁移](docs/advanced.md) · [排错](docs/troubleshooting.md) · [开发](docs/development.md) · [验证](docs/validation.md)

旧服务与部署资料仅供原部署维护参考：[固定提交 e50a7b6 的完整仓库](https://github.com/PargosAks/MaiBot-Codex-Image/tree/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272)。历史说明不是 0.7.0 安装入口。
