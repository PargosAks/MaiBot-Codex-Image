# 部署排错

先判断问题发生在安装、插件授权、网络连接还是绘图阶段。以下命令以 Linux root、宿主机源码 `/opt/maibot/plugins/codex_image`、数据目录 `/var/lib/maibot-codex-image`、容器 `maibot-core` 为例，使用前替换为实际值。首次安装见 [快速开始](quickstart.md)。

## 按现象定位

| 现象 | 优先检查 |
| --- | --- |
| `root 后台服务要求路径由 root 所有...` | [程序和父目录的属主与权限](#root-执行仍提示属主或权限错误) |
| `venv/.lock` 不安全、`VIRTUAL_ENV` 不匹配 | [旧安装器与 uv 环境锁](#uv-锁文件与环境警告) |
| `CalledProcessError`、工作目录不是绝对路径 | [systemd 校验](#systemd-校验或启动失败) |
| 当前用户未获绘图授权 | [QQ 白名单和插件重载](#qq-已授予命令权限仍被拒绝) |
| 宿主机访问 401，容器访问超时 | [Docker 网络与防火墙](host-service.md#docker-容器连接超时) |
| 服务连接中断、结果可能未知 | [保留原任务并排查连接](#连接中断或结果未知) |

排错时保留原配置、共享密钥、Codex 登录、完整任务和图片目录、插件发送记录。不要删除数据或重新生成密钥来“重新安装”。不要贴出 `secret.txt`、登录文件或完整配置；日志也应先脱敏。

## root 执行仍提示属主或权限错误

以 root 执行安装不代表解压出来的每个文件和目录都属于 root。安装器检查执行文件、源码、环境及其相关目录，要求属主 UID 为 0，且组和其他用户不可写；`755` 本身不违规，但 UID 1001 会被拒绝。以报错中的实际路径为准。

先只读查看。下面仅是本次 VPS 的 Codex 路径示例，其他版本请使用自己的路径：

```sh
CODEX_BIN='/root/.codex/packages/standalone/releases/0.156.1-x86_64-unknown-linux-musl/bin/codex'
stat -L -c 'UID=%u 属主=%U 权限=%a 路径=%n' -- "$CODEX_BIN" "$(dirname -- "$CODEX_BIN")"
namei -l "$CODEX_BIN"
```

本次实际发现：修正 `codex` 文件后，上一级 `bin` 目录仍属于 UID 1001，因此脚本继续拒绝。若核实你自己的这两个路径也应由 root 管理，可针对它们修正属主，再运行脚本：

```sh
chown root:root -- "$CODEX_BIN" "$(dirname -- "$CODEX_BIN")"
bash setup.sh
```

若只有组或其他用户写权限异常，先确认用途，再对报错的单一路径移除对应写权限；不要给整个目录树套用统一权限。不要递归 `chown` / `chmod` 整个 `~/.codex`，不要关闭检查。已有 `755` 路径无需再改权限。

## uv 锁文件与环境警告

旧安装器把服务环境根目录的 `venv/.lock` 判为可写源码，这是已在 **0.6.2** 修复的误报。uv 在 Unix 上会有意创建 `0666` 协调锁。新脚本只区分这个指定位置的普通、root 所属、无执行位锁文件，其他路径保护仍有效。

在宿主机插件源码目录更新并原地重试，保留旧文件：

```sh
cd /opt/maibot/plugins/codex_image
git pull --ff-only
bash setup.sh
```

无需修改锁文件权限、删除虚拟环境或任务目录。未配对初装只留下空目录和空 `.service.lock` 时可以继续；已有任务数据却缺少原密钥，或已配对配置丢失时，应恢复原文件，不能新建密钥掩盖问题。

旧日志中的 `VIRTUAL_ENV=... does not match ... and will be ignored` 是配置工具临时环境与服务目标环境不同的警告，不等于安装失败。0.6.2 已清理子进程继承的该变量，继续显式选择服务环境；不要加 `--active` 把服务依赖装进临时工具环境。

## systemd 校验或启动失败

旧版只显示 `CalledProcessError`，无法定位阶段；**0.6.3** 会显示失败阶段、固定命令名称和退出码。本次 VPS 的实际错误是：

```text
WorkingDirectory= path is not absolute: "/opt/maibot/plugins/codex_image"
Unit configuration has fatal error, unit will not be started.
```

生成器对 `WorkingDirectory` 错加了包围引号。0.6.3 已修复，正确输出如下；`ExecStart`、`Environment` 有各自的引号规则，不要全局去引号：

```ini
WorkingDirectory=/opt/maibot/plugins/codex_image
```

更新源码后重跑 `bash setup.sh`。首次安装尚未启动的服务不需要预先停止；已有运行服务时，先确认在途任务，按 [服务维护说明](quickstart.md#日常升级) 处理。不要跳过单元校验。

使用默认数据目录时，可先在宿主机运行以下只读命令：

```sh
systemd-analyze verify /var/lib/maibot-codex-image/runtime/maibot-codex-image.service
systemctl show maibot-codex-image.service -p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus
systemctl status maibot-codex-image.service --no-pager --full
journalctl -u maibot-codex-image.service -n 80 --no-pager
```

`verify` 应无本单元致命错误。若另有 snapd 或 1Panel 单元的 `Unknown key ... ignoring` 警告，先看是否还有本服务的致命错误，不要因此修改其他服务。

`LoadState=not-found`、`ActiveState=inactive` 时，即使 `Result=success` 或 `ExecMainStatus=0` 也不能认定启动成功。应核对 `Loaded: loaded`、`Active: active (running)`、应用启动完成日志和监听地址。`enabled` 只表示配置了开机自启，实际重启恢复仍需单独验证。

脚本安装的 systemd 单元位于 `/etc/systemd/system/maibot-codex-image.service`；`runtime/` 中的是校验和生成用文件。常规插件更新与白名单修改不需要操作它们。

## QQ 已授予命令权限仍被拒绝

MaiBot 命令权限和插件白名单需要同时通过。插件按真实发送者的 `platform:user_id` 精确匹配：

- 在 WebUI 的 `connection.allowed_users` 列表中添加一项 `qq:123456789`，替换为发送命令者的 QQ 号。
- 使用英文冒号 `:`，不要用 `qq：123456789`、裸数字、群号或机器人 QQ。
- 列表编辑器逐项填写；直接编辑 TOML 时才使用 `allowed_users = ["qq:123456789"]`。
- 给该用户授予 `codex_image_command`，允许插件 `send.text` / `send.image`；保存后 [重载插件](quickstart.md#如何重载插件)。

MCP 服务端不需要填写 `allowed_users`，也不需要重新运行安装脚本。新装插件的空列表 `[]` 默认拒绝所有人。WebUI 本地会话的身份不一定是 QQ 身份，不要直接沿用 QQ 号判断本地测试权限。

## 连接中断或结果未知

“服务连接中断，结果可能未知”可能出现在诊断、提交或查询阶段，单凭这句话不能断定任务已经创建，也不能断定没有创建。

1. 先确认当时执行的命令；`/画图诊断` 自身不会绘图。保存原命令、时间和已返回的 `img_...` 编号。
2. Docker 部署先做 [网络排查](host-service.md#docker-容器连接超时)。容器直连未认证端点得到 HTTP 401 后，再用原账号执行 `/画图诊断` 核对带密钥的 MCP 调用。
3. 已有编号：在原用户、原聊天中查询 `/画图状态 任务编号`。完成但未收图时，先核对聊天记录和发送状态，再使用 `/重发画图 任务编号` 取原图。
4. 提交时断线且没有编号：先核查原时间段的服务任务记录和脱敏日志，确认是否被接受；不要再发一条相同绘图命令试探，它可能成为新任务。

没有确认原任务状态前，不重启 MCP、不删除任务或发送记录、不重复提交。HTTP 401、诊断通过、真实 Codex 绘图完成和聊天实际收图，是不同层次的验证。
