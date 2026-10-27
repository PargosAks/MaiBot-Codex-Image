# 手动配置外部服务

本页仅适用于可选的外部 MCP 模式。0.2.0 新安装默认内置服务，通常只需按根目录 README 手动登录并授权，无需本页步骤。以下以同一台 Windows 机器为例；目录请替换成自己的位置。

## 1 手动登录 Codex

按 [官方说明](https://learn.chatgpt.com/docs/codex/cli) 安装 CLI。确认 codex --version 可运行，并用 Get-Command codex.exe 查询原生客户端路径。

默认复用现有登录，在 PowerShell 执行；已经登录则无需再次登录：

```powershell
codex login status
# 仅未登录时执行：codex login --device-auth
```

使用 ChatGPT 订阅账号登录，结果应为 Logged in using ChatGPT。服务配置 home 指向该用户的 CODEX_HOME 或 ~/.codex。可选专用目录时，先设置 CODEX_HOME 再手动登录；无需复制凭证或删除 config.toml，绘图配置会单独隔离。

## 2 准备服务

进入插件仓库目录，在 PowerShell 执行：

```powershell
uv sync --project service --locked --no-dev --python 3.13
Copy-Item service\config.example.toml 'D:\MaiBotImageService\service.toml'
```

修改复制后的 service.toml：binary 填实际 codex.exe 路径，home 填现有登录目录，state 填独立任务目录，allowed_users 填实际平台与用户 ID；model 可留空。Windows TOML 路径建议使用正斜线。

例如：

```toml
binary = "C:/实际路径/codex.exe"
home = "~/.codex"
state = "D:/MaiBotImageService/state"
allowed_users = ["qq:123456789"]
model = ""
host = "127.0.0.1"
port = 8765
```

生成至少 32 字符的随机共享密钥，自行保管。在当前启动服务的 PowerShell 中设置对应环境变量：

```powershell
$imageSecret = & '.\service\.venv\Scripts\python.exe' -c 'import secrets; print(secrets.token_urlsafe(40))'
$env:CODEX_IMAGE_SECRET = $imageSecret
uv run --project service --locked --no-dev codex-image-mcp --config 'D:\MaiBotImageService\service.toml'
```

重开终端后需重新提供同一个密钥，不要每次启动都生成新密钥。若希望自动保管和配对，请使用可选部署工具。

## 3 配置插件

在 MaiBot WebUI 插件设置中填写：

- connection.url：http://127.0.0.1:8765/mcp。
- connection.secret：与服务启动环境变量相同的密钥。
- connection.allowed_users：与服务相同的实际用户列表。

插件配置示例见仓库根目录 config.example.toml。然后在 MaiBot 命令权限中给对应用户授权本插件的绘图命令，并重启 MaiBot。

## 4 验证

发送 /画图 和描述，等待任务编号和图片。保留 MCP 服务窗口，关闭服务后插件无法新建任务或取图。

Linux/macOS 的步骤一致：设置 CODEX_HOME，用官方 CLI 手动登录，uv sync 准备服务环境；设置 CODEX_IMAGE_SECRET 后启动服务。原生 codex 路径可由 command -v codex 查询。

容器与跨机器配置见 [进阶配置](advanced.md)。
# 适用范围

本页介绍可选的**外部 MCP 服务模式**。0.2.0 新安装默认内置服务，通常只需按根目录 README 手动登录并授权，无需本页步骤。

外部模式请在插件配置增加：

```toml
[service]
mode = "external"
```

