# MaiBot-Codex-Image

## 项目介绍

在 MaiBot 中通过本机 Codex CLI 使用 ChatGPT 订阅绘图，并将图片发回聊天。内置 MCP 服务随插件自动启动和停止，无需修改 MaiBot 本体。

默认复用已有 Codex 登录，绘图进程使用独立配置；专用登录目录可选。

由 Codex 进行开发。

## 目前能做到什么

| 命令 | 功能 |
| --- | --- |
| /画图 描述 | 生成一张图片并自动发送 |
| /画图状态 任务编号 | 查询任务进度和结果 |
| /重发画图 任务编号 | 发送已生成的图片，不重新绘图 |
| /画图诊断 | 检查服务与登录状态，不生成图片 |

绘图使用 Codex 订阅额度。目前不支持改图、参考图、批量生成或自然语言自动触发。Windows 本地会话已验证，VPS 与其他聊天平台待实际测试。

## 如何安装部署

需要 MaiBot 1.3.x、SDK 2.16.x / 2.17.x、Python 3.12+、官方 Codex CLI，以及具备 Codex 绘图权限的 ChatGPT 账号。

在 MaiBot 实例的 plugins 目录安装，以下以 Linux / VPS 为例：

~~~sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git codex_image
cd codex_image
codex login status
~~~

使用与运行 MaiBot 相同的系统用户。已有 ChatGPT 登录无需重登；未登录时手动运行 `codex login --device-auth`。也可用 `python3 login.py --status` 检查插件默认读取的目录，Windows 将 python3 换成 python。

也可下载仓库 ZIP，解压为 plugins/codex_image。重启 MaiBot 加载插件，Python 依赖见 _manifest.json 与 requirements.txt，需安装到 MaiBot 实际使用的环境。当前尚未上架插件中心。

**容器部署时，Codex CLI 需要安装在 MaiBot 所在容器内。**

## 如何配置

1. 在插件设置中填写 `connection.allowed_users`，格式为平台加真实用户 ID，例如 `qq:123456789`。
2. 在 MaiBot 中为这些用户授予本插件 `codex_image_command` 命令权限，并允许插件发送文字和图片。
3. 保持 `service.mode = "auto"`、`connection.secret = ""`，保存后重新加载插件。
4. 输入 `/画图诊断`，确认正常后发送 `/画图 一只戴耳机的猫，白色背景`。

也可编辑插件的 config.toml，基础配置如下，其余字段使用默认值：

~~~toml
[plugin]
config_version = "0.3.0"
enabled = true

[service]
mode = "auto"

[connection]
allowed_users = ["qq:123456789"]
secret = ""
~~~

`codex.home` 留空时，优先复用 MaiBot 进程的 `CODEX_HOME`，未设置则使用当前系统用户的 `~/.codex`。已有 config.toml 可以保留，绘图不继承其中的 Provider、MCP、钩子或模型设置。认证读取与刷新交由原 Codex 客户端管理，绘图进程只使用内存中的访问令牌。

可选专用目录：设置 `codex.home = "/你的路径/codex-image-home"`，再运行 `python3 login.py --home "/你的路径/codex-image-home" --device-auth`。之前已在 `~/.codex-maibot-image` 登录的用户，也可把这个路径填入 codex.home 继续使用。

若找不到客户端，设置 `codex.binary` 为原生程序路径，并用 `python3 login.py --binary "客户端路径"` 登录。自定义登录目录时，`login.py --home "目录"` 必须与 `codex.home` 一致。

任务超时或断线后，请按原编号查询，避免重复提交。

## 配置项说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| plugin.config_version | `"0.3.0"` | 配置版本，通常无需修改 |
| plugin.enabled | `true` | 是否启用插件 |
| service.mode | `"auto"` | auto：密钥为空时使用内置服务，否则沿用外部服务；embedded：强制内置；external：外部服务 |
| connection.allowed_users | `[]` | 允许使用的用户列表；为空时拒绝所有人 |
| codex.binary | `""` | 留空从 PATH 检测；也可填写原生 Codex 程序路径，不接受 .cmd / .ps1 |
| codex.home | `""` | 留空复用 CODEX_HOME 或 ~/.codex；填写路径时使用该目录的登录 |
| codex.model | `""` | 留空使用隔离绘图客户端默认模型，不继承日常模型设置 |
| codex.timeout | `300` | 单次绘图超时，单位秒 |
| codex.rpc_timeout | `30` | Codex RPC 请求超时，单位秒 |
| connection.poll_seconds | `2` | 任务查询间隔，单位秒 |
| connection.watch_seconds | `360` | 自动等待并取图的最长时间，单位秒；超时后可手动查询 |
| connection.url | `"http://127.0.0.1:8765/mcp"` | 仅外部模式使用的 MCP 地址 |
| connection.secret | `""` | 仅外部模式手动配置，至少 32 字符且与服务一致；内置模式自动生成内部密钥 |

完整示例见 [config.example.toml](config.example.toml)，独立服务部署见 [外部服务指南](docs/manual-setup.md)，其他设置见 [进阶配置](docs/advanced.md)。
