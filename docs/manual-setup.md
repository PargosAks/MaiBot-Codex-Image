# 手动部署独立 MCP 服务（Windows）

插件与安装工具为 0.6.3，独立服务保持 0.5.0，尚无 Release。插件仅保存连接设置，不负责启动服务或管理 Codex。以下是同一台 Windows 机器直接运行 MaiBot 的手动安装示例；Docker / Linux 首次安装见 [快速指南](quickstart.md)，详细手动步骤见 [宿主机指南](host-service.md)，旧安装先看 [迁移说明](advanced.md#从旧版本迁移)。Windows 不支持 `--systemd`。

## 1. 准备仓库和已有登录

需要 Git、uv、Python 3.12+ 和已安装的官方 Codex CLI。使用将运行 MCP 的系统用户，在 PowerShell 中执行，路径换成自己的位置：

```powershell
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git 'D:\MaiBot\plugins\codex_image'
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git 'D:\MaiBotImageService'
Set-Location 'D:\MaiBotImageService'
uv sync --project service --no-dev
codex login status
```

已有 ChatGPT 登录无需重新登录；未登录才手动执行 `codex login --device-auth`。插件依赖另需安装到 MaiBot 的实际环境，见插件 `_manifest.json` 和 `requirements.txt`。

同机部署可只克隆插件一份，改为进入 `D:\MaiBot\plugins\codex_image` 复用其中的 `service/`，后续相对路径均以该目录为准。服务仍是独立进程，运行数据放在源码目录外。

## 2. 配置服务

在所选仓库根目录，仅在尚无配置时复制模板：

```powershell
if (-not (Test-Path -LiteralPath '.\service.toml')) {
    Copy-Item -LiteralPath '.\service\config.example.toml' -Destination '.\service.toml'
}
```

编辑 `service.toml`，Windows 的 TOML 路径建议使用正斜线：

```toml
binary = ""
home = ""
state = "D:/MaiBotImageData/state"
model = ""
host = "127.0.0.1"
port = 8765
```

`binary` 省略或留空时自动查找原生 Codex；找不到时可用 `Get-Command codex.exe` 查询，并填写实际原生程序路径，不要填写 `.cmd` 或 `.ps1` 启动脚本。`home` 省略或留空时优先使用服务进程的 `CODEX_HOME`，未设置则选该用户目录下的 `.codex`；显式路径优先，升级时保留已有值。TOML 不会展开字面量 `$env:CODEX_HOME`。

服务端不再配置用户白名单；旧 `service.toml` 中的 `allowed_users` 读取时会警告并忽略，建议删除。服务仍校验 Bearer、HMAC 签名、有效期、scope 完整性与任务归属，持钥调用方视为可信。

专用登录目录可选；若使用它，先在手动登录的终端设置对应 `CODEX_HOME`，再将同一路径填入 `home`。不复制凭证、不覆盖或删除原 Codex `config.toml`。服务其他配置见 [进阶说明](advanced.md#宿主机服务配置)。

## 3. 复用密钥并启动

在仓库根目录首次生成共享密钥，文件存在时复用；限制该文件只供运行服务的用户访问：

```powershell
if (-not (Test-Path -LiteralPath '.\secret.txt')) {
    $imageSecret = & '.\service\.venv\Scripts\python.exe' -c 'import secrets; print(secrets.token_urlsafe(40))'
    Set-Content -LiteralPath '.\secret.txt' -Value $imageSecret -Encoding ascii
}
$env:CODEX_IMAGE_SECRET = (Get-Content -LiteralPath '.\secret.txt' -Raw).Trim()
$imageConfig = (Resolve-Path -LiteralPath '.\service.toml').Path
uv run --project service --no-dev codex-image-mcp --config $imageConfig
```

密钥至少 32 字符，只通过环境变量提供给服务，不写入 `service.toml`。重开终端后读取同一文件再启动，不要重新生成密钥。不要输出或提交它。

也可不设置 `CODEX_IMAGE_SECRET`，改设 `CODEX_IMAGE_SECRET_FILE` 为同一个 UTF-8 密钥文件的绝对路径。直接密钥变量存在时优先，空值也不会回退到文件。

保留运行窗口；关闭即停止服务。需要后台常驻时交给已有进程管理器，并明确同一个系统用户、密钥环境和配置绝对路径。每个 `state` 只运行一个实例；插件重载、MaiBot 重启均不会代为启动服务。

## 4. 配对插件

在 MaiBot 插件设置或插件 `config.toml` 中填写：

```toml
[plugin]
config_version = "0.6.3"
enabled = true

[connection]
url = "http://127.0.0.1:8765/mcp"
secret = "填写服务 secret.txt 中的完整密钥"
allowed_users = ["qq:123456789"]
poll_seconds = 2
watch_seconds = 360
```

只有插件 `connection.allowed_users` 控制谁可发请求，空列表拒绝所有人。另在 MaiBot WebUI 给对应用户授予本插件 `codex_image_command` 命令权限及 `send.text` / `send.image` 能力，保存后重新加载插件。插件不再提供服务模式或 Codex 配置项。

## 5. 验证

先执行 `/画图诊断` 检查 MCP 连接与授权；它不检查 Codex 登录，也不生成图片。需要实际绘图验收时，发送一次 `/画图` 和描述，记录任务编号并核对状态、自动发图及聊天中实际显示的图片。

若超时、断线或结果未知，使用原编号查询；不要重复提交，也不要通过删除数据库强制重试。历史 Windows 结果不等于 0.6.3 安装流程验收，见 [验证记录](validation.md)。
