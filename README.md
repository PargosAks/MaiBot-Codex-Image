# MaiBot-Codex-Image

## 1. 项目介绍

在 MaiBot 中通过 Codex CLI 使用 ChatGPT 订阅绘图，并将图片发回聊天。0.4.0 使用宿主机独立 MCP 服务：宿主机运行 `service/` 和 Codex，MaiBot 插件只负责连接、提交任务和发图。

由 Codex 进行开发。

## 2. 目前能做到什么

| 命令 | 功能 |
| --- | --- |
| `/画图 描述` | 生成一张图片并自动发送 |
| `/画图状态 任务编号` | 查询原任务进度 |
| `/重发画图 任务编号` | 发送已有图片，不重新绘图 |
| `/画图诊断` | 检查 MCP 连接，不生成图片；登录在绘图时检查 |

绘图使用 Codex 订阅额度。目前不支持改图、参考图、批量生成或自然语言自动触发。本版已通过本机独立服务真实绘图测试；Docker、VPS 和 MaiBot 实际聊天收图待验收，详见 [验证记录](docs/validation.md)。

## 3. 如何安装部署

需要 MaiBot 1.3.x、SDK 2.16.x / 2.17.x；宿主机另需 Git、uv、Python 3.12+、已安装的官方 Codex CLI 和具备绘图权限的 ChatGPT 登录。

新安装分为插件和宿主机服务两份仓库，以下是路径示意：

```sh
# 在 MaiBot 的实际插件目录安装；Docker 使用已有的插件持久化目录机制。
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git /你的MaiBot实例/plugins/codex_image
# 在宿主机准备独立服务。
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git ~/maibot-codex-image-host
cd ~/maibot-codex-image-host
uv sync --project service --no-dev
```

MaiBot 直接部署且同一用户可访问仓库时，也可复用插件仓库里的 `service/`，仍需单独启动服务。插件依赖需安装到 MaiBot 实际使用的环境，见 `_manifest.json` 和 `requirements.txt`。

在宿主机用运行服务的系统用户执行 `codex login status`。服务 `home` 留空时默认沿用该用户的 `CODEX_HOME` 或 `~/.codex` 登录；已有登录无需重登，未登录时手动执行 `codex login --device-auth`。

继续按 [Linux / Docker 宿主机指南](docs/host-service.md) 或 [Windows 手动部署](docs/manual-setup.md) 配置并启动服务。无需修改 MaiBot 本体、Dockerfile、Compose 或重建镜像。旧版本升级先看 [迁移说明](docs/advanced.md#从旧版本迁移)。

## 4. 如何配置

宿主机使用独立的 `service.toml`，密钥通过 `CODEX_IMAGE_SECRET` 提供。按指南准备配置后，在宿主机仓库根目录启动并保持运行：

```sh
uv run --project service --no-dev codex-image-mcp --config /你的绝对路径/service.toml
```

插件配置只保留以下字段，密钥至少 32 字符且与宿主机一致：

```toml
[plugin]
config_version = "0.4.0"
enabled = true

[connection]
url = "http://127.0.0.1:8765/mcp"
secret = "填写宿主机使用的随机共享密钥，至少32字符"
allowed_users = ["qq:123456789"]
poll_seconds = 2
watch_seconds = 360
```

两端白名单填写相同的真实平台与用户 ID，并在 MaiBot 中授予这些用户本插件 `codex_image_command` 命令权限及文字、图片发送权限，保存后重新加载插件。Codex 路径、登录目录和模型均在宿主机配置。

Docker bridge 容器应使用实际宿主机网桥 Gateway 地址，不能照用容器内的 `127.0.0.1`；服务监听地址须与插件 URL 匹配，具体见部署指南。

## 5. 插件配置项说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.config_version` | `"0.4.0"` | 配置版本 |
| `plugin.enabled` | `true` | 是否启用插件 |
| `connection.url` | `"http://127.0.0.1:8765/mcp"` | 宿主机 MCP 地址 |
| `connection.secret` | `""` | 必填；至少 32 字符，与服务一致 |
| `connection.allowed_users` | `[]` | 允许用户；空列表拒绝所有人 |
| `connection.poll_seconds` | `2` | 任务查询间隔，秒 |
| `connection.watch_seconds` | `360` | 自动等待取图的最长时间，秒 |

## 6. 使用与帮助

先运行 `/画图诊断`，再按需发送 `/画图 一只戴耳机的猫，白色背景`。诊断成功只证明 MCP 连接与授权，不证明 Codex 登录或真实绘图成功。超时或断线后按原编号查询，避免重复提交；超过自动等待时间后可查询并手动取图。

服务不会随插件加载或容器重启自动启动；Linux 后台常驻与开机自启见 [systemd 配置](docs/host-service.md#7-systemd-后台运行与开机自启)。登录复用、数据保留、迁移及高级可选工具见 [进阶说明](docs/advanced.md)，开发检查见 [开发指南](docs/development.md)。
