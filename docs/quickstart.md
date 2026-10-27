# 新人快速开始

主路径：**Linux 宿主机运行绘图服务，Docker 中的 MaiBot 加载薄插件**。安装工具负责配对配置；最后由你在 MaiBot WebUI 授权、重载并验证。

本版为 **0.5.0 源码版**，不需要下载 Release。实际 Linux + Docker 部署仍需按下述步骤在目标机验证，历史测试范围见 [验证记录](validation.md)。

## 1. 准备宿主机

- MaiBot 已运行，版本为 1.3.x，SDK 为 2.16.x / 2.17.x。
- 宿主机已安装 Git、uv 和官方 Codex CLI；服务环境需要 Python 3.12+，由 uv 准备。
- 使用**将运行服务的当前系统用户**操作，并手动完成 Codex 的 ChatGPT 登录。普通 root VPS 的 `--systemd` 路径全程使用 root；不要中途换用户，以免换了登录目录。
- 确认 MaiBot 使用的**宿主机插件挂载源路径**，并准备真实允许用户，例如 `qq:123456`。插件依赖需在 MaiBot 实际环境可用，声明见 [_manifest.json](../_manifest.json) 和 [requirements.txt](../requirements.txt)。

不熟悉 uv 时，先看 [uv 官方安装指南](https://docs.astral.sh/uv/getting-started/installation/)。Linux 可使用官方安装命令，完成后重开终端并检查 `uv --version`：

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Codex 请按 [官方 CLI 安装指南](https://developers.openai.com/codex/cli/) 安装。登录方式见 [官方认证说明](https://developers.openai.com/codex/auth/)。**有 ChatGPT 订阅不保证有绘图权限**，以账号实际可用能力为准。

## 2. 准备仓库、登录和服务环境

在宿主机执行，仓库不要放进容器临时目录：

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git ~/maibot-codex-image-host
cd ~/maibot-codex-image-host
codex login status
```

已有 ChatGPT 登录直接继续；仅未登录时手动执行以下命令，并按提示在浏览器完成登录。设备码登录需账号或工作区允许，详见官方认证说明。

```sh
codex login --device-auth
codex login status
```

确认是 ChatGPT 登录后，按服务锁文件准备环境；安装工具不会代替这一步：

```sh
uv sync --project service --locked --no-dev --python 3.13
```

## 3. 确认挂载，运行安装工具

先确认容器名和挂载，下面的 `maibot` 换成实际容器名：

```sh
docker ps --format '{{.Names}}'
docker inspect --type=container maibot --format '{{json .Mounts}}'
```

`Source` 是宿主机路径，`Destination` 是容器内路径。**必须确认插件目录确实位于供 MaiBot 加载的挂载中**；例如宿主机 `/srv/maibot/plugins` 挂载到容器插件目录，填写的插件安装目录可为 `/srv/maibot/plugins/codex_image`。不要填容器内路径，也不要把任意宿主机目录当成挂载源。工具会读取 `docker inspect` 再验证，不会替你新增挂载。

目标路径末级必须为 `codex_image` 且父目录已存在，服务仓库与目标插件目录不能互相包含；首次会复制薄插件代码，不要提前向目标再 clone 第二份，旧插件版本不匹配时脚本会拒绝覆盖，请先备份并更新插件或查看 [迁移说明](advanced.md#从旧版本迁移)。

重复执行配置前，先用 `systemctl stop maibot-codex-image.service` 或在前台窗口按 `Ctrl+C` 停止正在运行的宿主机 MCP，以免服务目录锁导致配置被拒绝。

普通 **Linux root VPS + 系统级 Docker + systemd**，在宿主机仓库根目录执行：

```sh
uv run --project service --locked --no-dev python scripts/setup_host.py --systemd
```

缺少必要参数时按提示填写：

| 提示 | 填写内容 |
| --- | --- |
| 容器名 | 实际 MaiBot 容器名；留空表示同机直接部署 |
| 宿主机插件目录 | MaiBot 实际可加载的插件安装目录，如 `/srv/maibot/plugins/codex_image` |
| 允许用户 | 真实 `平台:用户ID`，如 `qq:123456`；仅写入插件 `connection.allowed_users` |
| 网络选择 | 容器有多个网络时，选择实际使用的网络 |

工具会选择容器实际网络的宿主机 Gateway，使服务监听地址与插件 URL 匹配；不要照抄示例网关 IP，也不要把容器内 `127.0.0.1` 当作宿主机。

工具还会用只读 `docker exec ... id` 获取容器默认执行用户，为私有插件配置设置相应 UID/GID，并检查嵌套挂载遮蔽。远程 Docker、rootless 或 user namespace 映射请使用手动部署；自定义入口另行切换 MaiBot 运行用户时，需要手动核对配置读取权限。

若插件配置/源码另有单文件挂载，或已有私有插件目录对容器用户不可读，工具会停止，不会写入被遮蔽的文件或放开私有目录权限；这类已有部署请按手动指南核对。

root 后台服务要求源码、虚拟环境及启动程序不能被其他用户改写，工具会先检查属主和权限。检查失败时，以 root 在 `/root` 或合适的 `/opt` 目录重新准备宿主机仓库与环境；不要直接让后台服务执行其他用户可写的源码，也不要更改 MaiBot 容器用户来绕过检查。

`--systemd` 是可选项，会安装并启用 systemd 服务，服务使用当前用户。它仅支持 Linux root 和系统级 Docker，不支持 Windows 或 rootless Docker。普通用户、无需后台服务或不符合条件时，去掉该选项：

```sh
uv run --project service --locked --no-dev python scripts/setup_host.py
```

默认会打印前台启动命令；按输出在宿主机启动并保留窗口，关闭窗口即停止服务。使用 systemd 时按输出核对服务状态。不要让前台和后台实例同时使用同一个任务目录；服务运行不等于已完成绘图验收。

### 工具会做什么

- 在 uv 已准备好的服务环境中，发现宿主机原生 Codex 和已有登录目录；默认复用当前用户的 `CODEX_HOME` 或 `~/.codex`。
- 读取 `docker inspect` 验证插件挂载并选择实际 Gateway；直接部署使用本机连接。
- 首次生成并配对 `secret.txt`、`service.toml` 和插件 `config.toml`，使两端密钥与地址一致；允许用户仅写入插件 `connection.allowed_users`，插件配置版本为 `0.5.0`。
- 新目录只复制薄插件运行文件，不复制宿主机服务源码；目标已是同 ID 源仓库时只配配置，并要求版本一致。
- 重复配置保留既有密钥、任务和日常 Codex 配置。发现已有配置不一致会拒绝，不覆盖或搬迁；缺少密钥且已有状态时会拒绝自动换新。

工具**不会**修改 MaiBot、Dockerfile、镜像、Compose、防火墙或 MaiBot 授权，不自动登录 Codex，也不发送测试绘图。现有安装或迁移请先看 [进阶说明](advanced.md#从旧版本迁移)，遇到拒绝时核对原配置，不要删除密钥、任务库或发送记录来绕过检查。

允许谁发请求只由插件 `connection.allowed_users` 控制。服务仍校验 Bearer、HMAC 签名、有效期、scope 完整性与任务归属，持钥调用方视为可信。旧 `service.toml` 若含 `allowed_users`，读取时会警告并忽略，建议删除该旧项；无需在服务端另配白名单。

## 4. WebUI 授权并重载插件

在 MaiBot WebUI 确认插件已加载、依赖可用，并给白名单中的对应用户授予：

- 本插件 `codex_image_command` 命令权限。
- `send.text` 和 `send.image` 能力。

保存后**重载插件**。插件白名单不能替代这一步；安装工具不修改这些权限。

## 5. 先诊断，再只画一次

用已授权的账号在 MaiBot 聊天中发送：

```text
/画图诊断
```

它只检查 MCP 连接与授权，不检查 Codex 登录，不生成图片。诊断通过后，仅提交一次真实绘图，并记录返回的任务编号：

```text
/画图 一只戴耳机的猫，白色背景
```

核对任务状态、自动发图和聊天中实际显示的图片。超时、断线或结果未知时，用原编号查询；已完成但未收到图片时尝试取回原图，不重新画：

```text
/画图状态 任务编号
/重发画图 任务编号
```

发送结果未知时先查聊天记录，不要反复重发。诊断通过、进程运行和真实聊天收图是不同检查；历史结果见 [验证记录](validation.md)。

## 同机直接部署与 Windows

**Linux 同机直接部署：**仍先完成仓库、ChatGPT 登录和 `uv sync`，运行工具时容器名留空，插件目录填写 MaiBot 实际使用的宿主机目录。服务独立运行，本机连接使用 `127.0.0.1`。已有同 ID、同版本源仓库可以只配配置；随后按前述 WebUI 和验证步骤完成。手动配置见 [宿主机指南](host-service.md)。

**Windows：**按 [Windows 手动部署指南](manual-setup.md) 准备和启动独立服务；不要使用 `--systemd`。该指南面向同机直接运行 MaiBot，不能直接套用到 Docker Desktop。Docker Desktop、rootless 和特殊网络需另外核对挂载及可达地址，不属于本文主路径。

## 按参数安装

仍在宿主机仓库根目录执行。以下容器名、网络名、路径和用户 ID 都要替换为实际值；`--allowed-user` 可重复且只写入插件配置，多网络可用 `--network` 明确选择：

```sh
uv run --project service --locked --no-dev python scripts/setup_host.py \
  --container maibot \
  --network maibot_default \
  --plugin-dir /srv/maibot/plugins/codex_image \
  --allowed-user qq:123456 \
  --allowed-user qq:789012 \
  --systemd
```

其他选项可查看帮助：

```sh
uv run --project service --locked --no-dev python scripts/setup_host.py --help
```

详细手动流程和 systemd 日常管理见 [宿主机指南](host-service.md)，配置字段、登录复用与数据保留见 [进阶说明](advanced.md)。
