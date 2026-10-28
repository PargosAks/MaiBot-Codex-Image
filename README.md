# MaiBot-Codex-Image

## 1. 项目介绍

让 MaiBot 调用宿主机上的 Codex CLI 绘图，并将图片发回聊天。一份源码放在 MaiBot 插件目录，宿主机独立运行 MCP 服务；支持直接部署和 Docker 部署，无需修改 MaiBot 本体或 Docker 镜像。

由 Codex 进行开发。

## 2. 目前能做到什么

| 命令 | 功能 |
| --- | --- |
| `/画图 描述` | 生成一张图片并自动发送 |
| `/画图状态 任务编号` | 查询原任务进度 |
| `/重发画图 任务编号` | 发送已有图片，不重新绘图 |
| `/画图诊断` | 检查 MCP 连接，不生成图片；登录在绘图时检查 |

目前不支持改图、参考图、批量生成或自然语言自动触发。绘图使用账号额度，实际可用能力以账号为准。

插件与安装工具版本为 **0.6.3**，兼容独立 MCP 服务 **0.5.0**；服务源码、依赖与协议保持不变。各版本验证范围见 [验证记录](docs/validation.md)，Linux / Docker 实际部署待验证，尚未发布 Release。

## 3. 如何安装部署

宿主机先准备 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和已手动完成 ChatGPT 登录的 Codex CLI。MaiBot 需为 1.3.x，SDK 为 2.16.x / 2.17.x。

将下面的 `/opt/maibot/plugins` 替换为 **MaiBot 实际使用的宿主机插件目录**。Docker 用户应使用现有插件挂载的宿主机路径；在宿主机执行：

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git /opt/maibot/plugins/codex_image
cd /opt/maibot/plugins/codex_image
bash setup.sh
```

按提示填写 **容器名（直接部署留空）**；多个网络时选择网络。脚本不询问允许用户。Linux root 默认配置 systemd 后台运行及开机自启；需要前台运行时用 `bash setup.sh --foreground`。最后在 MaiBot WebUI 配置允许用户、授予命令与发图权限，保存并重载插件。

已有目录不要 clone 覆盖。已有独立 MCP 的用户升级看下文，无需重新配置。详细步骤与适用条件见 [快速开始](docs/quickstart.md)。

0.6.2 已修复 uv 环境锁权限误报；安装中断后保留原文件，按 [原地重试说明](docs/quickstart.md#安装中断后重试) 继续。

## 4. 如何配置

脚本自动配对地址与密钥，复用宿主机当前服务用户的 `CODEX_HOME` 或 `~/.codex` 登录，不复制登录文件。新安装的服务配置、密钥、任务和环境放在宿主机独立数据目录，插件 `config.toml` 留在源码根目录且不纳入 Git。

在 MaiBot WebUI 的插件面板设置 `connection.allowed_users`，填写真实 `platform:user_id`（例如 `qq:123456789`，使用英文冒号）；还需授权对应用户的 `codex_image_command` 命令，以及插件的 `send.text` / `send.image` 能力。保存后重载插件。新安装默认 `[]`，配置前拒绝所有用户，无法绘图。已有名单由 WebUI 维护，脚本不校验、改写或清空，即使为空或曾填错也不因此阻塞安装；格式错误请在面板修正。脚本不代替登录或授权。

```toml
[plugin]
config_version = "0.6.3"
enabled = true

[connection]
url = "http://127.0.0.1:8765/mcp"
secret = "填写宿主机使用的随机共享密钥，至少32字符"
allowed_users = [] # 在 MaiBot 插件面板添加允许用户
poll_seconds = 2
watch_seconds = 360
```

Docker bridge 的地址由脚本根据现有网络选择，不能照抄容器内的 `127.0.0.1`。已有安装保留原 URL、密钥、登录和任务路径，详见 [进阶说明](docs/advanced.md)。

## 5. 配置项说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.config_version` | `"0.6.3"` | 插件配置版本；已有配置按兼容规则读取 |
| `plugin.enabled` | `true` | 是否启用插件 |
| `connection.url` | `"http://127.0.0.1:8765/mcp"` | 宿主机 MCP 地址，由脚本配对 |
| `connection.secret` | `""` | 必填；至少 32 字符，与服务一致 |
| `connection.allowed_users` | `[]` | 在 MaiBot 插件面板配置真实 `platform:user_id`；空列表拒绝所有人 |
| `connection.poll_seconds` | `2` | 任务查询间隔，秒 |
| `connection.watch_seconds` | `360` | 自动等待取图的最长时间，秒 |

宿主机服务字段、数据目录和脚本参数见 [进阶说明](docs/advanced.md)。

## 6. 升级与使用

**仅插件变动、MCP 协议兼容时**，在插件源码目录执行：

```sh
git pull --ff-only
```

然后在 MaiBot WebUI **重载插件**即可，独立 MCP 无需重启，也无需重新运行配置脚本。不要删除旧宿主机服务仓库，它可能仍是运行入口。若更新涉及 MCP 源码、依赖、监听地址或密钥，需另行停止服务、核对原配置后重新配置并启动；服务不支持源码热更新，见 [升级说明](docs/quickstart.md#日常升级)。

先用 `/画图诊断` 检查连接，再提交一次 `/画图 一只戴耳机的猫，白色背景` 并核对实际收图。超时或结果未知时用原编号查询，避免重复绘图。部署验证与真实聊天收图需分别确认。
