# MaiBot-Codex-Image

## 1. 项目介绍

在 MaiBot 中连接宿主机 Codex CLI，使用 ChatGPT 登录请求绘图，并将图片发回聊天。宿主机运行独立的 `service/` 和 Codex，MaiBot 插件负责连接、提交任务和发图。

由 Codex 进行开发。

## 2. 目前能做到什么

| 命令 | 功能 |
| --- | --- |
| `/画图 描述` | 生成一张图片并自动发送 |
| `/画图状态 任务编号` | 查询原任务进度 |
| `/重发画图 任务编号` | 发送已有图片，不重新绘图 |
| `/画图诊断` | 检查 MCP 连接，不生成图片；登录在绘图时检查 |

目前不支持改图、参考图、批量生成或自然语言自动触发。绘图会使用账号额度；有 ChatGPT 订阅不代表必然有绘图权限，以账号实际可用能力为准。

本版为 **0.5.0**，代码与配置版本同步提升，尚无 Release。**实际 Linux + Docker 安装尚未验收**；历史测试范围见 [验证记录](docs/validation.md)，旧版本结果不代表本版通过。

## 3. 如何安装部署

首次安装请看 [新人快速开始](docs/quickstart.md)。支持 MaiBot 1.3.x、SDK 2.16.x / 2.17.x；宿主机先准备 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和已手动完成 ChatGPT 登录的 [Codex CLI](https://developers.openai.com/codex/cli/)。

普通 Linux root VPS 上执行：

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git ~/maibot-codex-image-host
cd ~/maibot-codex-image-host
codex login status
uv sync --project service --locked --no-dev --python 3.13
uv run --project service --locked --no-dev python scripts/setup_host.py --systemd
```

按提示填写 **MaiBot 容器名、宿主机插件目录、允许用户**。脚本安装薄插件、自动配对 MCP 配置与密钥，并设置后台运行及开机自启；最后在 MaiBot WebUI 授权并重载插件。

`--systemd` 需要 Linux root；普通用户可去掉此项，按输出前台启动。同机直接部署时容器名留空，Windows 见 [手动指南](docs/manual-setup.md)。脚本不修改 MaiBot 本体或 Docker 镜像，不代替 Codex 登录；已有安装先看 [迁移说明](docs/advanced.md#从旧版本迁移)。

## 4. 如何配置

安装工具配对宿主机 `secret.txt`、`service.toml` 与插件 `config.toml`；不要重新生成密钥。手动配置与启动见 [宿主机详细指南](docs/host-service.md)。

插件配置只保留以下字段，密钥至少 32 字符且与宿主机一致：

```toml
[plugin]
config_version = "0.5.0"
enabled = true

[connection]
url = "http://127.0.0.1:8765/mcp"
secret = "填写宿主机使用的随机共享密钥，至少32字符"
allowed_users = ["qq:123456789"]
poll_seconds = 2
watch_seconds = 360
```

**允许用户只在插件 `connection.allowed_users` 设置**，MCP 服务无需再配白名单。在 MaiBot WebUI 授权对应用户的绘图命令，并允许插件使用 `send.text` / `send.image`，保存后重载插件。

Codex 路径、登录目录和模型在宿主机配置；服务鉴权和旧配置兼容说明见 [进阶说明](docs/advanced.md)。

Docker bridge 容器应使用实际宿主机网桥 Gateway 地址，不能照用容器内的 `127.0.0.1`；服务监听地址须与插件 URL 匹配，具体见部署指南。

## 5. 插件配置项说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.config_version` | `"0.5.0"` | 配置版本 |
| `plugin.enabled` | `true` | 是否启用插件 |
| `connection.url` | `"http://127.0.0.1:8765/mcp"` | 宿主机 MCP 地址 |
| `connection.secret` | `""` | 必填；至少 32 字符，与服务一致 |
| `connection.allowed_users` | `[]` | 允许用户；空列表拒绝所有人 |
| `connection.poll_seconds` | `2` | 任务查询间隔，秒 |
| `connection.watch_seconds` | `360` | 自动等待取图的最长时间，秒 |

## 6. 使用与帮助

先运行 `/画图诊断`，通过后仅提交一次 `/画图 一只戴耳机的猫，白色背景`，核对聊天实际收图。诊断只检查 MCP 连接与授权，不证明 Codex 登录或真实绘图成功。超时、断线或结果未知时，用原编号查询状态，避免重复提交。

服务独立运行，不随插件重载启动。后台运行可选 `--systemd`，手动管理见 [systemd 指南](docs/host-service.md#7-systemd-后台运行与开机自启)。登录复用、数据保留与迁移见 [进阶说明](docs/advanced.md)，开发检查见 [开发指南](docs/development.md)。
