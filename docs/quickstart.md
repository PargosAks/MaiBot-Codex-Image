# 新人快速开始

**一份源码放进 MaiBot 插件目录，在宿主机运行一次 `bash setup.sh` 配置 MCP，再到 MaiBot WebUI 设置允许用户与权限，保存并重载插件。** MCP 服务独立运行，插件加载、重载和卸载不会启停它。

本指南对应插件与安装工具 **0.6.3**，宿主机 MCP 服务仍为 **0.5.0**。尚未发布 Release。Linux VPS 已有 systemd 启动和宿主机 HTTP 检查记录；Docker 连通、重启机器后的恢复和本版聊天收图仍待目标机验收，见 [验证记录](validation.md)。

## 1. 安装前准备

- MaiBot 已运行，版本为 1.3.x，SDK 为 2.16.x / 2.17.x。
- 宿主机已有 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和官方 Codex CLI，且已由你手动完成 ChatGPT 登录。
- 使用将运行 MCP 的系统用户操作。Linux root 默认安装系统级 systemd 服务，使用 root 原有登录；不要中途换用户或换 `CODEX_HOME`。

脚本使用 uv 准备隔离的配置工具环境和服务环境，不要求你提前手动同步 Python 依赖。它不会安装 Git、uv 或 Codex，不会代替你登录 Codex。

### 找到真实插件目录

直接部署时使用 MaiBot 实际加载的 `plugins` 目录。Docker 部署时，使用**现有插件挂载的宿主机 Source 路径**，脚本也在宿主机运行。需要核对时，在宿主机执行，`maibot` 换成实际容器名：

```sh
docker inspect --type=container maibot --format '{{json .Mounts}}'
```

例如宿主机 `/opt/maibot/plugins` 已挂载到容器插件目录，就把源码放在 `/opt/maibot/plugins/codex_image`。不要使用容器内路径，也不要新增挂载或把宿主机 Codex 登录目录复制到插件目录。

自动 Docker 配置针对本机 Linux、系统级 Docker 和常规可写插件挂载。远程 Docker、rootless、user namespace 映射和特殊挂载继续使用 [手动部署指南](host-service.md)。root 后台服务要求源码及其父目录不可被其他宿主机用户替换；若现有路径不符合检查，先核对部署权限，不要放宽权限绕过检查。

## 2. 安装源码并配置

下面以宿主机插件目录 `/opt/maibot/plugins` 为例，**先替换为你核实的路径**。目标末级使用 `codex_image`，父目录需已存在。在宿主机执行：

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git /opt/maibot/plugins/codex_image
cd /opt/maibot/plugins/codex_image
bash setup.sh
```

已有 `codex_image` 目录不要再次 clone 覆盖，转到 [日常升级](#日常升级)。也可将完整源码复制进该目录；以后希望用 Git 升级，首次应选择 clone。

按提示填写：

| 提示 | 填写内容 |
| --- | --- |
| 容器名 | MaiBot 实际容器名；同机直接部署留空 |
| 网络选择 | 容器有多个网络时，选择实际使用的网络 |

插件目录默认就是脚本所在源码根目录，无需再次填写。脚本不询问或强制填写允许用户。工具识别现有挂载、容器用户和实际 Gateway，配对两端地址与密钥；不改变 Compose、镜像、挂载、防火墙或 MaiBot 本体。自定义入口另行切换运行用户时，应手动核对插件配置的读取权限。

Linux root 默认安装并启动 `maibot-codex-image.service`，启用开机自启。可按需要选择：

```sh
bash setup.sh --foreground
```

该选项只配置并打印前台启动命令，按输出在宿主机执行并保留服务终端。也可显式使用 `bash setup.sh --systemd`；非 root 环境请使用前台或手动管理，systemd 自动配置只支持 Linux root。

### 配置和数据存在哪里

| 内容 | 新单目录安装的默认位置 |
| --- | --- |
| 插件与 MCP 源码 | 同一份 `plugins/codex_image` |
| 插件配置 | 源码根目录 `config.toml`，Git 忽略 |
| 宿主机配置和密钥 | 数据目录中的 `service.toml`、`secret.txt` |
| 宿主机任务与图片 | 数据目录中的 `runtime/state` |
| 服务虚拟环境 | 数据目录中的 `venv` |
| Codex 登录 | 当前服务用户原有的 `CODEX_HOME` 或 `~/.codex` |
| 插件发送记录 | MaiBot Host 的 `ctx.paths.data_dir`，沿用原目录 |

Linux root 的新数据目录默认 `/var/lib/maibot-codex-image`；其他用户默认 `${XDG_DATA_HOME:-$HOME/.local/share}/maibot-codex-image`，可用 `--data-dir` 指定。不要把宿主机数据目录或 Codex 凭据放进 Docker 可见的插件挂载。

已有配置继续使用原密钥、登录和完整任务路径。仅更新插件配置版本或重复配对，且服务配置、启动单元和环境均无需变化时，可以保留 MCP 运行；工具不改写服务配置或密钥。**更改服务配置、启动单元或首次准备服务环境时，需先停止服务。** 已配对且 `venv` 存在时不会重复同步；未完成初装的重试会重新检查缓存环境，普通插件升级仍无需运行脚本。

## 3. WebUI 设置允许用户与权限

在 MaiBot WebUI 确认插件与依赖可用，打开插件面板：

1. 在 `connection.allowed_users` 添加真实 `platform:user_id`，例如 `qq:123456789`；将示例数字换成发送命令者的 QQ 号，使用英文冒号 `:`。其他平台填写其实际平台标识和用户 ID。
2. 给对应用户授予 `codex_image_command` 命令权限，并允许插件使用 `send.text`、`send.image`。
3. 保存设置并重载插件。

QQ 名单每项填写 `qq:你的QQ号`，使用英文冒号；不能只填数字、中文冒号 `qq：`、机器人 QQ 或群号。在列表编辑器中逐项添加，不要将整个 TOML 数组粘贴成一项。直接编辑配置文件时才写 `allowed_users = ["qq:123456789"]`。

新安装白名单默认为 `[]`，拒绝所有用户，完成上述配置前无法绘图。白名单不能替代 MaiBot 权限；MCP 不需要配置用户白名单。

已有安装的名单保持原样，脚本不校验、改写或清空；即使为空或曾填错格式，也不会因此阻塞安装。请在 WebUI 修正名单，保存并重载后再测试。

`connection.url` 和 `connection.secret` 优先保留脚本已配对的值。Docker 示例：实际 Gateway 为 `172.23.0.1` 时 URL 为 `http://172.23.0.1:8765/mcp`。无需再到 MaiBot 原生 MCP 配置页添加同一服务。

### 如何重载插件

先保存配置，再在 WebUI 的“Codex 订阅绘图”配置面板右上角，将“启用”开关关闭后重新打开。刷新网页本身不会重载插件。不要为了应用白名单或插件源码更新重启宿主机 MCP。

也可由具有插件管理权限的账号在聊天中执行：

```text
/pm plugin reload pargosaks.maibot-codex-image
```

这是 MaiBot 内置插件管理命令，其管理权限与 `codex_image_command` 绘图命令权限不同。普通绘图用户不必拥有管理权限。

## 4. 手动验证

用已授权账号在 MaiBot 聊天中发送：

```text
/画图诊断
```

诊断只检查 MCP 连接和授权，不验证 Codex 登录或真实绘图。Docker 部署若诊断报“服务连接中断”，先做 [容器到宿主机网络检查](host-service.md#docker-容器连接超时)，不要反复提交绘图。通过后仅提交一次，记录任务编号：

```text
/画图 一只戴耳机的猫，白色背景
```

核对任务进度和聊天中实际显示的图片。超时或断线时查询原任务，已完成但未收到图片时取回原图：

```text
/画图状态 任务编号
/重发画图 任务编号
```

发送结果未知时先查聊天记录，不要反复重发或删除发送记录。若提交时断线且未收到编号，不能断定服务未接受任务；保留原消息和时间，核查原任务后再决定下一步，见 [结果未知的处理](troubleshooting.md#连接中断或结果未知)。

## 日常升级

**更新仅涉及插件且 MCP 协议兼容时**，在已有插件源码目录执行：

```sh
git pull --ff-only
```

随后 [重载插件](#如何重载插件)，不需要重启独立 MCP，不需要重跑 `setup.sh`。插件重建连接后继续访问原服务的任务和图片。Git 若报告冲突或无法快进，先核对本地改动，不要强制重置。

本次 0.6.3 更新保留 0.5.0 服务源码、`service/pyproject.toml` 和 `service/uv.lock`，无需重启兼容的 MCP；版本说明应区分插件和服务。后续更新若涉及 MCP 源码、依赖、监听地址或共享密钥，则属于服务维护：等待在途任务处理完，停止服务，核对并保留原配置、密钥、Codex 登录与任务路径，再重新配置和启动。不要把 `git pull` 当成服务热更新。

**已使用独立 MCP 的旧安装：**直接更新插件源码、重载插件，继续连接原 URL 和密钥即可；无需运行新 `setup.sh`，无需迁移数据。旧宿主机服务仓库可能仍被 systemd 引用，不要删除。若确需改由新源码启动服务，先看 [迁移说明](advanced.md#从旧版本迁移)，不能直接套用新安装路径。

## 按参数安装

在宿主机插件源码目录执行，将容器名与网络名替换为实际值。允许用户仍在 WebUI 配置：

```sh
bash setup.sh \
  --container maibot \
  --network maibot_default
```

| 参数 | 用途 |
| --- | --- |
| `--container` | 指定 MaiBot 容器，未提供时交互询问 |
| `--network` | 指定容器已有网络 |
| `--data-dir` | 指定宿主机服务配置与数据目录 |
| `--plugin-dir` | 兼容旧分离布局；新安装默认源码根目录，无需填写 |
| `--foreground` | 选择前台模式 |
| `--systemd` | 显式选择 Linux root 系统级服务 |

旧 `--allowed-user` 参数仅为兼容保留，是可选的首次白名单预填入口，可重复；普通安装无需提供，也不会覆盖已有名单。日后修改名单使用 WebUI。

不要同时选择前台和 systemd。完整参数可用 `bash setup.sh --help` 查看；Windows、特殊 Docker 环境和手动服务管理见 [手动指南](manual-setup.md) 与 [宿主机指南](host-service.md)。这些指南中的旧分离布局命令不是本版新用户的必经步骤。

## 安装中断后重试

如果旧脚本在 `venv/.lock` 权限检查处退出，这是安装器误报，0.6.2 已修复。在插件源码目录执行：

```sh
git pull --ff-only
bash setup.sh
```

保留原配置和数据，无需更改锁文件权限或删除虚拟环境、任务目录。尚未配对的初装仅留下空状态目录和空 `.service.lock` 时可继续；若已有数据库、图片或其他状态却缺少原密钥，脚本仍会停止，需恢复原密钥。

其他权限错误会显示实际 UID 与权限。Codex 程序、源码和目录仍须满足 root 后台执行要求，应针对报错路径核对，不递归修改整个 Codex 登录目录。

如果旧脚本仅显示 `CalledProcessError`，说明它没有显示具体失败阶段，不能据此认定缺少权限或依赖。0.6.3 会显示失败命令名称与退出码；同时修正了生成单元的 `WorkingDirectory` 引号问题。

可在宿主机执行以下只读检查，默认路径示例中的数据目录按实际值替换：

```sh
systemd-analyze verify /var/lib/maibot-codex-image/runtime/maibot-codex-image.service
systemctl show maibot-codex-image.service -p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus
```

核对实际命令输出后再处理。不要删除密钥或任务目录来绕过失败；也不要将 `secret.txt`、Codex 登录文件或完整插件配置粘贴到排错记录。

详细的 root 属主检查、uv 锁误报、systemd 校验和 QQ 授权问题见 [部署排错](troubleshooting.md)。容器访问超时见 [宿主机网络排查](host-service.md#docker-容器连接超时)。
