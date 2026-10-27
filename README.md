# MaiBot Codex Image

在 MaiBot 中输入 **/画图**，使用本机 Codex 的 ChatGPT 订阅生成图片，再自动发回当前聊天。

**0.2.0 起内置 MCP 服务，默认随插件自动启动和停止。已安装 Codex CLI 的机器上，只需手动登录并配置使用权限，无需单独启动服务、选择端口或配对密钥。**

**本项目由 OpenAI Codex（AI 编程助手）按 PargosAks 的需求完成开发，包含 MaiBot 插件、MCP 服务、部署工具和测试。项目由 PargosAks 持有和维护。**

## 功能

| 命令 | 作用 |
| --- | --- |
| /画图 描述 | 生成一张图片，完成后自动发回当前聊天 |
| /画图状态 img_任务编号 | 查询进度和结果 |
| /重发画图 img_任务编号 | 发送已有图片，不重新生成 |
| /画图诊断 | 检查 MCP 与 Codex 登录，不生成图片 |

新绘图会使用 Codex 订阅额度。暂不支持改图、参考图、取消、批量、多账号和自然语言自动触发。

## 安装方式

这是一个标准 MaiBot 插件仓库：根目录包含 plugin.py、_manifest.json 和 LICENSE。**插件本身不需要额外安装脚本。**

### 手动安装

在 MaiBot 实例的 plugins 目录中克隆公开仓库：

~~~sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git codex_image
~~~

也可以在 GitHub 选择 **Code → Download ZIP**，解压为 plugins/codex_image；或通过 MaiBot WebUI 的 ZIP 安装功能安装纯插件包。重启 MaiBot 后，配置项会出现在插件设置中。

MCP 服务已随插件打包，由插件加载、配置更新和卸载接口管理。运行关系：

~~~text
MaiBot 绘图插件 → 自动启动的内置 MCP 服务 → 已手动登录的 Codex CLI
~~~

### 插件中心安装

计划支持，当前尚未提交上架。MaiBot 官方插件中心要求公开 GitHub 仓库、已发布的 Release 和审核收录；完成审核收录后，用户即可在插件中心搜索安装。仓库公开不等于已经上架。

收录后，插件中心安装会同时带上内置 MCP 源码；Python 依赖通过标准 manifest 声明。Codex CLI 是需要预先安装的外部客户端，账号登录仍由用户手动完成。

## 首次使用：登录和授权

需要 MaiBot 1.3.x、SDK 2.16.x 或 2.17.x、Python 3.12+、本机官方 Codex CLI，以及有 Codex 内置绘图权限的 ChatGPT 账号。MaiBot 必须能找到客户端；容器部署时，客户端需要安装在同一容器内。

**Codex 登录由你手动完成。插件和部署工具不会索取 API Key、复制日常 Codex 凭证或自动登录。**

### 1. 手动登录 Codex

使用与运行 MaiBot 相同的操作系统用户，在插件目录执行：

~~~sh
python login.py
~~~

没有浏览器的机器可以执行 `python login.py --device-auth`。检查是否已登录：`python login.py --status`。这里的 Python 只需要标准库，也可用 `uv run --no-project --python 3.13 login.py`。

插件使用独立的登录目录 **~/.codex-maibot-image**。这与日常 Codex 登录目录不同，需要手动登录一次；不会复制日常账号凭证。此目录不能放 config.toml。

如果客户端不在 MaiBot 的 PATH 中，设置插件的 `codex.binary` 为原生程序绝对路径，并使用 `python login.py --binary "客户端路径"` 登录。自定义登录目录时，`login.py --home "目录"` 与插件 `codex.home` 必须一致。

### 2. 配置使用权限

在 MaiBot 插件设置中，把 `connection.allowed_users` 设置为真实平台与用户 ID，例如 `["qq:123456789"]`；空列表拒绝所有人。同时在 MaiBot 的命令权限中，为这些用户授权本插件的 **codex_image_command**，并允许插件发送文字和图片。不需要授予全局管理权限。

保留 `service.mode = "auto"`、`connection.secret = ""`。`connection.url` 在内置模式下不使用。保存配置后重新加载插件，或重启 MaiBot。

### 3. 检查并画图

先输入 `/画图诊断`，确认服务和登录正常，再使用 `/画图 描述`。诊断只读取账号，不创建绘图会话。

MCP 只监听本机随机端口。内部密钥、任务数据库与图片保存在 MaiBot 分配的插件数据目录下，不保存在插件源码目录，因此插件升级不会覆盖这些数据。

原有外部 MCP 用户：`auto` 模式遇到已配置的共享密钥，会沿用外部服务，保留原有任务访问方式。跨机器部署和手动独立服务配置见 [外部服务指南](docs/manual-setup.md)。

普通聊天模型与这条绘图链路独立；无需为了绘图给普通聊天 Provider 添加图像模型，也无需把本服务注册到 Codex 的 MCP 列表。

## 可选：全新机器部署工具（独立服务模式）

普通插件安装无需运行本节脚本。完整部署包保留之前的一键工具，用于准备新 MaiBot、下载缺失客户端和部署独立服务；它会显式配置 `service.mode = "external"`。**若希望服务随插件启动，请使用前面的默认内置模式。**

Windows PowerShell，在解压后的项目目录执行，替换实例目录和 QQ 号：

~~~powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -MaiBotDirectory 'D:\MaiBot' -InstallDirectory 'D:\MaiBotImageService' -AllowedUser 'qq:123456789'
~~~

如果还没有 MaiBot，加 **-CreateInstance**，即可安装官方 MaiBot 1.3.15 并创建实例：

~~~powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -CreateInstance -MaiBotDirectory 'D:\MaiBot' -InstallDirectory 'D:\MaiBotImageService' -AllowedUser 'qq:123456789'
~~~

Linux/macOS，在解压目录执行：

~~~sh
sh ./install.sh --maibot-directory "$HOME/MaiBot" --install-directory "$HOME/MaiBotImageService" --allowed-user 'qq:123456789'
~~~

没有实例时增加 **--create-instance**。脚本检测 uv、Python 和 Codex，缺少时准备在独立服务目录；下载的官方 Codex 完整包固定版本并校验 SHA256。已有客户端时优先使用现有原生程序，也可显式传入 CodexBinary 或 --codex-binary。

安装成功后，Windows 在服务目录依次运行：

~~~powershell
powershell -ExecutionPolicy Bypass -File .\Login-Codex.ps1
powershell -ExecutionPolicy Bypass -File .\Check-Installation.ps1
powershell -ExecutionPolicy Bypass -File .\Start-Service.ps1
~~~

Linux/macOS：

~~~sh
sh "$HOME/MaiBotImageService/login-codex.sh"
sh "$HOME/MaiBotImageService/check-installation.sh"
sh "$HOME/MaiBotImageService/start-service.sh"
~~~

登录脚本可附加 **--device-auth** 用于无浏览器环境。检查脚本只验证客户端与登录，不执行绘图。保留服务窗口，再正常启动 MaiBot。由工具创建的 MaiBot 可使用生成的 Start-MaiBot 脚本启动；已有实例使用原来的启动方式。

首次 MaiBot 协议确认、WebUI 密码和普通聊天模型设置由用户自行完成。

## 独立服务部署工具的目录和权限

MaiBotDirectory 必须指向含 config、data、plugins 的实例目录。服务目录与实例目录必须分开，不能互相嵌套。

部署工具只操作独立服务目录、实例 plugins/codex_image 和本绘图命令的授权配置，**不修改 MaiBot 本体源码、安装包程序文件或普通聊天模型配置**。默认仅为指定用户授权绘图命令，不授予其他插件管理权限。使用 -NoGrantCommand 或 --no-grant-command 后，工具不改 bot_config.toml，改由你在 WebUI 授权。

允许用户格式为平台加真实 ID，例如 **qq:123456789**。WebUI 本地会话则是 **webui:实际用户ID**，与 QQ 号和昵称不同，请从会话信息或实例日志核对。

多个用户：Windows 使用 -AllowedUser 'qq:123','qq:456'；Unix 重复填写 --allowed-user。

独立 MCP 默认只监听 127.0.0.1:8765。若端口被占用，Windows 使用 -Port 18765，Unix 使用 --port 18765。内置模式自动选择随机端口，无需配置。容器或跨机器部署需要自行设置受限网络和可达地址，不能把容器自身 localhost 当作宿主机；见 [进阶配置](docs/advanced.md)。

## 开始画图

在获授权的聊天输入：

~~~text
/画图 一只戴蓝色耳机的机器人猫，白色背景，不要文字
~~~

麦麦先返回 img_ 开头的任务编号，生成后自动发送图片。发生超时或断线时，保留编号并查询原任务，不要立即重复发起新绘图。

## 更新和备份

内置模式更新时，正常停止或卸载插件，更新后重新加载即可；插件会关闭自己拥有的 MCP 监听和 Codex 子进程。插件中心更新也应使用 MaiBot 标准更新流程。不要覆盖正在运行的文件。

备份 MaiBot 的 `data/plugins/pargosaks.maibot-codex-image` 和专用 Codex 登录目录。未确认结果的任务不会自动重新生成；停止时仍在绘图的任务需按原编号查询。

独立模式更新前先停止 MCP 服务和 MaiBot。手动安装时更新仓库并重启；使用部署工具时，用新解压包执行原安装命令。

重复部署保留登录目录、共享密钥、任务和图片数据，不自动轮换密钥或清空发送记录。覆盖前备份受管插件文件和配置到服务目录 backups。已有已配置插件却找不到原服务目录时，工具会停止，避免静默替换密钥。

专用 codex-home、secret.txt、插件 config.toml 和服务运行目录含凭证，不要上传到 GitHub 或发到群里。备份时同时保留服务目录和 MaiBot 实例。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 提示需要 ChatGPT 登录 | 内置模式运行 python login.py，再输入 /画图诊断；独立模式使用 Login-Codex |
| 提示无权限 | 核对实际平台和用户 ID，检查绘图命令权限和两端白名单 |
| 无法连接 MCP | 内置模式先用 /画图诊断，检查插件日志并重新加载；独立模式检查 Start-Service、地址与密钥 |
| 找不到 Codex | 为运行 MaiBot 的用户安装客户端；或在 codex.binary 填写原生程序路径 |
| outcome_unknown | 结果未确认，先核查原任务；服务不会自动重画 |
| 图片已生成但未发送 | 使用原任务编号取图；发送结果未知时先检查聊天 |
| CodexBinary 是 .cmd/.ps1 | 改为原生 codex.exe，或让部署工具下载官方完整包 |
| 普通聊天报模型错误 | 另行配置 MaiBot 普通聊天模型；本项目只负责绘图 |

## 验证和开发

Windows、MaiBot 1.3.15、SDK 2.17.0 已完成 **0.2.0 内置 MCP 自动启动、真实登录诊断、真实订阅绘图和本地会话自动发图**，浏览器图片与生成产物哈希一致。MaiBot 本体 789 个文件核验未修改；详见 [验证记录](docs/validation.md)。

此前外部服务模式的基础绘图已获用户手动确认；新版内置模式的用户验收另行进行。Linux/macOS 真机及 QQ 等外部平台仍待联调。

开发、测试与构建见 [开发说明](docs/development.md)。

## 许可与来源

本项目采用 [MIT](LICENSE) 许可。上游 Codex 与 MaiBot 按各自许可、服务条款和账号权限使用。

- [MaiBot 官方部署](https://docs.maibot.chat/manual/deployment/uv-tool)
- [MaiBot 插件中心提交规范](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)
- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)
- [Codex 登录](https://learn.chatgpt.com/docs/auth)

