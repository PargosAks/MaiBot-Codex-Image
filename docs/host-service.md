# 宿主机独立 MCP 部署（Linux / Docker）

首次安装优先看 [新人快速开始](quickstart.md)：完整源码放进插件目录，在宿主机执行 `bash setup.sh`，Linux root 默认设置 systemd。插件与安装工具为 0.6.3，独立服务仍为 0.5.0；仅插件升级无需重启兼容 MCP。脚本不要求允许用户，装好后在 MaiBot 插件面板设置白名单。以下第 1～7 节是可选的手动部署及旧分离布局管理流程，不是新用户的必经步骤。工具不自动登录、不改 MaiBot 授权，最后仍需 WebUI 授权与重载。尚无 Release；本次 VPS 已确认宿主机服务启动，Docker 连通与真实绘图收图仍待验证。Windows 见 [手动指南](manual-setup.md)。

**已用 `setup.sh` 安装的用户不要照第 3、4、7 节另建配置、密钥或覆盖服务文件。** Linux root 新安装默认使用以下路径；已有安装或 `--data-dir` 指定过目录时，以原配置为准：

| 内容 | `setup.sh` 默认布局 |
| --- | --- |
| 源码 | MaiBot 实际插件目录；本次 VPS 为 `/opt/maibot/plugins/codex_image` |
| 服务配置、密钥 | `/var/lib/maibot-codex-image/service.toml`、`secret.txt` |
| 任务与图片 | `/var/lib/maibot-codex-image/runtime/state` |
| 服务环境 | `/var/lib/maibot-codex-image/venv` |
| 生成的 unit / 已安装的 unit | `/var/lib/maibot-codex-image/runtime/maibot-codex-image.service` / `/etc/systemd/system/maibot-codex-image.service` |

服务运行问题见 [服务状态与日志](#宿主机服务状态与日志)，宿主机可访问而容器超时见 [Docker 容器连接超时](#docker-容器连接超时)。root 属主、uv `.lock`、systemd 安装错误、QQ 授权及未知任务的处理见 [故障排查](troubleshooting.md)。

0.5.0 的允许用户仅在插件 `connection.allowed_users` 配置；服务不再维护白名单。旧 `service.toml` 的 `allowed_users` 读取时会警告并忽略，建议删除旧项。服务仍校验 Bearer、HMAC、有效期、scope 完整性与任务归属，持钥调用方视为可信。

0.4.0 统一使用 external-only 架构。MaiBot 无论直接部署还是运行在 Docker 中，插件都连接单独运行的宿主机 `service/`：

```text
MaiBot 插件 → 宿主机 MCP 服务 → 宿主机 Codex
```

宿主机负责 Codex 登录、任务和图片；插件通过 MCP 取回图片，无需共享图片目录。无需修改 MaiBot 本体、Dockerfile、Compose、镜像，也无需在 MaiBot 容器安装 Codex、挂载凭证或授予 Docker 控制权限。

以下为手动分离布局的新安装示例，目录按实际用户的权限调整。这套示例把配置和密钥放在手动仓库内，与上表的 `setup.sh` 默认数据目录不同。升级时先看 [迁移说明](advanced.md#从旧版本迁移)，不要覆盖原服务配置、密钥或数据。

## 1. 准备源码（手动分离布局）

宿主机需要 Git、uv、Python 3.12+ 和已安装的官方 Codex CLI。使用计划运行 MCP 的系统用户执行：

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git ~/maibot-codex-image-host
cd ~/maibot-codex-image-host
uv sync --project service --no-dev
codex login status
```

已有 ChatGPT 登录无需重新登录；未登录时手动执行 `codex login --device-auth`。服务配置的 `home` 留空时复用这个用户当前的 `CODEX_HOME`，未设置时使用 `~/.codex`。不复制凭证，不删除日常 Codex 配置；专用登录目录可选。

在 MaiBot 实际使用的插件持久化目录另行安装插件：

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git /你的MaiBot实例/plugins/codex_image
```

该目录须已通过现有机制供 MaiBot 发现；不要仅在宿主机任意目录克隆后就假定容器会加载。将插件依赖安装到 MaiBot 实际使用的环境，声明见仓库根目录 `_manifest.json` 和 `requirements.txt`。

直接部署的 MaiBot 若与 MCP 在同机、仓库可被服务用户访问，可省略第二份仓库，复用 `plugins/codex_image/service/`，仍单独启动服务。运行数据应放在源码目录外，更新源码不会替代服务的数据备份。

## 2. 选择插件可达的监听地址

MaiBot 直接运行在宿主机，或容器已有 host 网络时，可以使用 `127.0.0.1`。

Linux Docker Engine bridge 网络下，先只读查看实际容器的网络信息，`maibot` 替换为容器名：

```sh
docker inspect --type=container maibot --format '{{json .NetworkSettings.Networks}}'
```

选取该容器实际连接网络的宿主机 `Gateway`，例如 `172.18.0.1`。**服务的 `host` 与插件 URL 均使用同一个实际 Gateway 地址和端口。** 不要照抄示例 IP，不要把容器内 `127.0.0.1` 当作宿主机。

当前服务会校验 HTTP Host：将监听改为 `0.0.0.0` 并不等于允许通过任意 IP 或域名访问。按本指南直接绑定选定 Gateway，可使监听与请求 Host 匹配。若需要防火墙放行，优先仅允许当前 MaiBot 容器 IP 访问该 Gateway 的服务端口，见 [Docker 容器连接超时](#docker-容器连接超时)；无需端口映射或开放公网端口，不根据网关地址猜测子网。

本指南不要求改 Compose 或添加 `host.docker.internal` 映射。Docker Desktop、rootless、无宿主机网桥地址及受限网络环境需另行核对可达地址与 Host 校验，不能直接套用此例。

## 3. 创建宿主机配置

在宿主机仓库根目录，仅在尚无配置时复制模板：

```sh
cp -n service/config.example.toml service.toml
```

编辑 `service.toml`。以下为 Docker bridge 示例，持久化路径和 Gateway 均需替换；`binary/home` 留空使用自动查找与已有登录：

```toml
binary = ""
home = ""
state = "/你的持久化目录/maibot-image/state"
model = ""
host = "172.18.0.1"
port = 8765
```

- `binary` 省略或留空时从 PATH 查找原生 Codex；找不到时填写实际原生可执行程序路径。`command -v codex` 可提供线索，若返回启动脚本，还需确认对应的原生程序。
- `home` 省略或留空时优先使用服务进程的 `CODEX_HOME`，否则使用该用户的 `~/.codex`；显式路径优先，升级时保留既有值。不要在 TOML 中填写字面量 `$CODEX_HOME`，它不会展开。
- `state` 使用可写的绝对路径；服务的任务数据库、产物和隔离绘图目录都保存在这里。
- 直接部署时将 `host` 改为 `127.0.0.1`，插件也用相应地址。

服务其余字段继续沿用模板或既有配置，见 [服务配置说明](advanced.md#宿主机服务配置)。

## 4. 提供密钥并启动

在宿主机仓库根目录首次生成随机密钥，已有文件时复用：

```sh
umask 077
if [ ! -f secret.txt ]; then
  ./service/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(40))' > secret.txt
fi
export CODEX_IMAGE_SECRET="$(cat secret.txt)"
uv run --project service --no-dev codex-image-mcp --config "$PWD/service.toml"
```

`CODEX_IMAGE_SECRET` 至少 32 字符，由环境变量读取，不写入 `service.toml`。保管好同一个密钥文件；不要每次启动生成新密钥，也不要将它贴入公开日志或提交到 Git。

也可不设置 `CODEX_IMAGE_SECRET`，改设 `CODEX_IMAGE_SECRET_FILE` 为该密钥文件的绝对路径。两者同时存在时，直接密钥变量优先，空值也不会回退到文件。密钥文件应为 UTF-8，不能把密钥本身填进文件路径变量。

前台关闭即停止服务。后台运行和开机自启见 [systemd 配置](#7-systemd-后台运行与开机自启)。每个 `state` 目录只启动一个服务实例；MaiBot 或容器重启不会代为启动宿主机 MCP。

## 5. 配对插件

在 MaiBot 插件设置或插件 `config.toml` 中填写：

```toml
[plugin]
config_version = "0.6.3"
enabled = true

[connection]
url = "http://172.18.0.1:8765/mcp"
secret = "填写宿主机 secret.txt 中的完整密钥"
allowed_users = ["qq:123456789"]
poll_seconds = 2
watch_seconds = 360
```

这是完整的插件配置范围，不再配置服务模式或本地 Codex。直接部署且服务监听 `127.0.0.1` 时，相应修改 URL。授予白名单用户本插件 `codex_image_command` 命令及文字、图片发送权限，保存并重新加载插件。

## 6. 分层验证

1. 宿主机以服务用户执行 `codex login status`，确保命令使用的登录目录与服务 `home` 一致。
2. 在 MaiBot 中执行 `/画图诊断`，检查 MCP 连接与授权；它不检查 Codex 登录，不创建绘图任务。
3. 需要验收真实绘图时，仅提交一次 `/画图 一个绿色三角形，白色背景`，记录任务编号，检查状态、自动发图和实际聊天中的图片。此步骤会使用订阅额度。

排查容器网络时，可利用容器已有 Python 发起一次不带密钥的请求，地址换成实际 Gateway：

```sh
docker exec maibot python3 -c 'import http.client; c=http.client.HTTPConnection("172.18.0.1",8765,timeout=5); c.request("GET","/mcp"); print(c.getresponse().status)'
```

返回 `401` 仅说明有可达的鉴权端点拒绝了未认证请求；Bearer 校验先于 MCP Host 检查，因此这还不能证明 Host 匹配、签名请求、登录或绘图成功。连接拒绝时检查服务和监听地址；超时检查网络与防火墙；带认证的请求出现 `403` 时核对 URL 的 Host 与监听配置。

容器请求超时的具体分层检查见 [Docker 容器连接超时](#docker-容器连接超时)。

## 7. systemd 后台运行与开机自启

本节仅用于手动分离布局。`setup.sh` 已管理同名服务时，跳到 [服务状态与日志](#宿主机服务状态与日志)，不要覆盖其 unit。手动部署先完成前面的配置与手动测试；本节以普通 Linux systemd、root 用户、仓库位于 `/opt/maibot-codex-image-host` 为例，复用现有 `service.toml`、`secret.txt` 和任务目录，不重新生成密钥。

若前面使用的是 `~/maibot-codex-image-host`，将本节所有服务目录替换为实际绝对路径，不必搬迁仓库或数据。若用其他系统用户登录 Codex，服务 `User`、`Group`、`HOME`、`PATH` 及文件访问权限也要对应调整。Docker 启动顺序配置适用于系统级 Docker；不直接套用 rootless Docker。

### 7.1 停止前台实例，固定程序与登录目录

先等待正在绘图的任务结束，在原前台窗口按 `Ctrl+C`。不要让前台实例和 systemd 同时使用同一个 `state`。

使用已登录 Codex 的 root 用户执行：

```sh
cd /opt/maibot-codex-image-host
uv sync --project service --locked --no-dev --python 3.13

unset CODEX_IMAGE_SECRET
export CODEX_IMAGE_SECRET_FILE="$PWD/secret.txt"

uv run --project service --locked --no-dev python - <<'PY'
import json
from pathlib import Path
from codex_image_mcp.config import Settings

settings = Settings.load(Path("service.toml"))
print("binary = " + json.dumps(settings.binary))
print("home = " + json.dumps(str(settings.home)))
PY
```

这只解析配置并显示路径，不登录或生成图片。编辑 `service.toml`，将 `binary`、`home` 替换成输出的绝对路径，保留其他字段；不要重复添加同名字段。这样 npm/nvm 安装的 Codex 和专用登录目录也不依赖 SSH 终端的 PATH 或 CODEX_HOME。

```sh
nano service.toml
chmod 600 secret.txt service.toml
```

### 7.2 创建服务文件

在同一个 SSH 窗口获取 uv 的实际路径，确认最后一行检查成功后继续：

```sh
UV_BIN="$(command -v uv)"
printf 'uv 路径：%s\n' "$UV_BIN"
test -n "$UV_BIN" && test -x "$UV_BIN"
```

`UV_BIN` 应为绝对可执行文件路径，例如 `/root/.local/bin/uv`，不能是 shell 别名或函数。下面未加引号的 `EOF` 会在创建文件时把该路径写入 `ExecStart`；systemd 无需继承这个 shell 变量。

已有同名服务时先备份，然后写入配置：

```sh
if [ -f /etc/systemd/system/maibot-codex-image.service ]; then
  cp -a /etc/systemd/system/maibot-codex-image.service \
    "/etc/systemd/system/maibot-codex-image.service.bak.$(date +%Y%m%d-%H%M%S)"
fi

cat > /etc/systemd/system/maibot-codex-image.service <<EOF
[Unit]
Description=MaiBot Codex Image MCP Service
Wants=network-online.target docker.service
After=network-online.target docker.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/maibot-codex-image-host

Environment="HOME=/root"
Environment="PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="CODEX_IMAGE_SECRET_FILE=/opt/maibot-codex-image-host/secret.txt"
UnsetEnvironment=CODEX_IMAGE_SECRET

ExecStart="$UV_BIN" run --project /opt/maibot-codex-image-host/service --locked --no-dev codex-image-mcp --config /opt/maibot-codex-image-host/service.toml

Restart=on-failure
RestartSec=5
TimeoutStopSec=90
KillMode=mixed
UMask=0077

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

- `CODEX_IMAGE_SECRET_FILE` 只记录密钥文件路径，不把密钥正文写进服务文件；`UnsetEnvironment` 避免旧的直接密钥变量覆盖它。
- `Restart=on-failure` 在异常退出后等待 5 秒重启；手动 `systemctl stop` 不触发自动重启。
- `KillMode=mixed` 先通知主进程停止，uv 在 Unix 下转发停止信号，最多等待 90 秒后清理剩余进程。
- `After` 规定启动顺序，不保证 Docker 网关或 Codex 已经可用。网关尚未建立时查看失败日志；服务异常退出会按重启策略再尝试。

若 MaiBot 直接部署且不使用 Docker，删除 `Wants`、`After` 中的 `docker.service`，保留 `network-online.target`。如果前台运行依赖代理或自定义 CA 环境变量，也需在服务中显式配置实际值；systemd 不会自动继承 SSH 会话环境，不要把含凭据的值贴到公开日志。

### 7.3 检查并启用开机自启

先检查配置；若报错，修正后再启动：

```sh
systemd-analyze verify /etc/systemd/system/maibot-codex-image.service
```

检查通过后执行：

```sh
systemctl daemon-reload
systemctl enable --now maibot-codex-image.service
systemctl status maibot-codex-image.service --no-pager -l
systemctl is-active maibot-codex-image.service
systemctl is-enabled maibot-codex-image.service
```

后两项预期分别为 `active`、`enabled`。此后断开 SSH 不会停止服务。无需为配置自启立即重启 VPS；真实开机恢复需在下次重启后检查，不能仅凭 `enabled` 视为已验证。

### 7.4 日志与日常管理

```sh
# 实时日志；Ctrl+C 只退出日志查看。
journalctl -u maibot-codex-image.service -f

# 最近 100 行。
journalctl -u maibot-codex-image.service -n 100 --no-pager

# 按需执行其中一条。
systemctl restart maibot-codex-image.service
systemctl stop maibot-codex-image.service
systemctl start maibot-codex-image.service
```

修改 systemd 服务文件后，先 `systemctl daemon-reload` 再重启服务。只修改 `service.toml` 或密钥文件内容时重启服务即可；两端共享密钥必须保持一致。

取消开机自启并停止，不删除配置、登录或任务：

```sh
systemctl disable --now maibot-codex-image.service
```

切换或重启服务尽量选在没有绘图进行时；结果未知的任务不会自动重画。`active` 仅表示进程运行，还需按第 6 节检查 `/画图诊断` 和实际聊天收图。

systemd 行为参考：[服务与重启策略](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[执行环境](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)、[进程停止方式](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)、[uv 信号处理](https://docs.astral.sh/uv/concepts/projects/run/#signal-handling)。

## 宿主机服务状态与日志

以下为宿主机上的只读检查，同时适用于脚本安装与手动部署：

```sh
systemctl status maibot-codex-image.service --no-pager --full
systemctl show maibot-codex-image.service \
  -p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus
systemctl is-enabled maibot-codex-image.service
journalctl -u maibot-codex-image.service -n 100 --no-pager
ss -lntp 'sport = :8765'
```

`LoadState=loaded`、`ActiveState=active`、`SubState=running`，配合应用启动完成和预期地址的监听，说明服务已启动；`enabled` 表示已配置开机自启。若服务 `not-found` 或 `inactive`，单独的 `Result=success`、`ExecMainStatus=0` 不能证明启动成功。systemd 验证报错时，先查看报错所属 unit；其他服务的 `ignoring` 警告不等于本服务失败原因，详见 [故障排查](troubleshooting.md)。

本次 VPS 已由用户提供日志确认：已安装的 unit 为 `loaded`、`enabled`，服务 `active (running)`，应用启动完成并监听 `172.23.0.1:8765`。这不等于容器可达、真实绘图发图或实际重启后的开机恢复已通过。仅升级兼容的插件无需重启 MCP；不要为排查容器网络反复重启正常运行的服务。

## Docker 容器连接超时

本节以本次 VPS 的实际布局为例：宿主机源码 `/opt/maibot/plugins/codex_image`，容器 `maibot-core`，网络 `maibot_default`，Gateway 为 `172.23.0.1`，容器 IP 为 `172.23.0.2`。**其他机器以及容器重建后，先重新检查地址再使用后面的命令。** 以下检查在宿主机执行，不需要改 MaiBot 本体、Docker 镜像、Compose 或现有挂载。

### 先核对监听与两端访问

```sh
docker inspect maibot-core --format \
  '{{range $name, $net := .NetworkSettings.Networks}}Network={{$name}} IP={{$net.IPAddress}} Gateway={{$net.Gateway}}{{println}}{{end}}'

ss -lntp 'sport = :8765'

curl --noproxy '*' --connect-timeout 3 --max-time 5 \
  -sS -o /dev/null -w 'HOST_HTTP=%{http_code}\n' \
  http://172.23.0.1:8765/mcp

docker exec maibot-core /MaiMBot/.venv/bin/python -c \
  'import http.client; c=http.client.HTTPConnection("172.23.0.1",8765,timeout=8); c.request("GET","/mcp"); print(c.getresponse().status)'
```

`/MaiMBot/.venv/bin/python` 是本次容器已有的 Python 路径；其他安装需替换成容器实际解释器路径，不需要为探测安装新依赖。请求不带密钥，也不创建绘图任务。

| 检查结果 | 含义与下一步 |
| --- | --- |
| 宿主机连接失败或未监听预期地址 | 先查服务状态、日志及 `service.toml` 的监听配置 |
| 宿主机 `401`，容器超时 | 服务在宿主机可达，继续查容器到宿主机的网络和入站防火墙 |
| 容器也返回 `401` | 未认证鉴权端点可达，再用 `/画图诊断` 检查插件的认证连接 |

**`401` 只证明未认证端点可达。** Bearer 拒绝发生在 MCP Host 检查之前，因此它不能证明 Host、共享密钥、HMAC 签名、Codex 登录或绘图成功。标准库探测超时也不足以认定 MCP SDK 版本或密钥有问题。

本次实测为宿主机 `HOST_HTTP=401`、容器 `TimeoutError timed out`。宿主机 `INPUT` 默认策略为 `DROP`，包含 `ufw-*` 与 `ts-input` 链；这些现象提示优先排查宿主机入站防火墙，但尚未定位具体丢弃规则，不能仅凭链名认定 UFW 已启用或已确认根因。

### 仅在 UFW 已启用时放行当前容器

先只读检查状态与现有规则：

```sh
ufw status verbose
iptables -nvL INPUT --line-numbers
```

**仅当 `ufw status verbose` 显示 `Status: active`，且上面已核实当前 IP 与 Gateway 仍分别是 `172.23.0.2`、`172.23.0.1` 时，才由管理员按需执行下面这条修改命令。** 它只允许当前容器 IP 访问该宿主机网关的 TCP 8765 端口：

```sh
ufw insert 1 allow in proto tcp \
  from 172.23.0.2 to 172.23.0.1 port 8765 \
  comment 'MaiBot MCP Docker'
```

这里访问的是宿主机自身监听的服务，使用入站 `allow in`，不需要 `route` 转发规则。`insert 1` 指 UFW 管理规则的第一条，不保证优先于所有自定义或 Tailscale 链。规则语法参考 [UFW 官方手册](https://manpages.ubuntu.com/manpages/focal/man8/ufw.8.html)。

无需为此启用、禁用、重置或重载整个 UFW；不要添加面向公网的 `8765/tcp` 放行，不修改 SSH、1Panel、Tailscale 或其他正常链路。若 UFW 为 `inactive` 或命令不存在，停止这一步，继续核对实际防火墙管理方式，不为套用本指南启用 UFW。不要从网关猜测 `/16` 等子网后整体放行。

执行过窄规则后，先查看并重新探测，不重复添加规则：

```sh
ufw status numbered
docker exec maibot-core /MaiMBot/.venv/bin/python -c \
  'import http.client; c=http.client.HTTPConnection("172.23.0.1",8765,timeout=8); c.request("GET","/mcp"); print(c.getresponse().status)'
```

若仍超时，保留当前状态及只读检查结果，进一步核对实际链与规则计数；不要关闭防火墙或扩大来源范围。容器重建可能改变 IP，届时需重新核对并调整这条窄规则。容器返回 `401` 后，再在 MaiBot 执行 `/画图诊断`；该诊断不会生成图片，真实绘图与收图另按第 6 节验收。

### 本次排查的验证边界

目前只确认宿主机服务启动与宿主机 `401`；尚未执行或确认上述 UFW 规则、容器返回 `401`、完整认证诊断及真实绘图收图。开机恢复也未通过实际重启验证。历史记录见 [验证记录](validation.md)。

若插件曾提示“服务连接中断，结果可能未知”，不要据此判定任务失败或重复提交绘图。保留已有原任务编号，连通恢复后按原编号查询；没有取得编号时也不要盲目重提，先核查原请求与服务记录。具体处理见 [故障排查](troubleshooting.md)。
