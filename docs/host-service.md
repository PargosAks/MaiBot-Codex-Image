# 宿主机独立 MCP 部署（Linux / Docker）

0.4.0 统一使用 external-only 架构。MaiBot 无论直接部署还是运行在 Docker 中，插件都连接单独运行的宿主机 `service/`：

```text
MaiBot 插件 → 宿主机 MCP 服务 → 宿主机 Codex
```

宿主机负责 Codex 登录、任务和图片；插件通过 MCP 取回图片，无需共享图片目录。无需修改 MaiBot 本体、Dockerfile、Compose、镜像，也无需在 MaiBot 容器安装 Codex、挂载凭证或授予 Docker 控制权限。

以下为新安装示例，目录按实际用户的权限调整。升级时先看 [迁移说明](advanced.md#从旧版本迁移)，不要覆盖原服务配置、密钥或数据。

## 1. 准备两份仓库

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

当前服务会校验 HTTP Host：将监听改为 `0.0.0.0` 并不等于允许通过任意 IP 或域名访问。按本指南直接绑定选定 Gateway，可使监听与请求 Host 匹配。仅允许实际 Docker 子网访问此端口，无需端口映射或开放公网端口。

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
allowed_users = ["qq:123456789"]
model = ""
host = "172.18.0.1"
port = 8765
```

- `binary` 省略或留空时从 PATH 查找原生 Codex；找不到时填写实际原生可执行程序路径。`command -v codex` 可提供线索，若返回启动脚本，还需确认对应的原生程序。
- `home` 省略或留空时优先使用服务进程的 `CODEX_HOME`，否则使用该用户的 `~/.codex`；显式路径优先，升级时保留既有值。不要在 TOML 中填写字面量 `$CODEX_HOME`，它不会展开。
- `state` 使用可写的绝对路径；服务的任务数据库、产物和隔离绘图目录都保存在这里。
- `allowed_users` 与插件一致。直接部署时将 `host` 改为 `127.0.0.1`，插件也用相应地址。

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

前台关闭即停止服务。正式运行可交给已有进程管理器，保持相同的系统用户、配置绝对路径、登录目录和密钥环境变量。每个 `state` 目录只启动一个服务实例；MaiBot 或容器重启不会代为启动宿主机 MCP。

## 5. 配对插件

在 MaiBot 插件设置或插件 `config.toml` 中填写：

```toml
[plugin]
config_version = "0.4.0"
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

本文是部署操作指南。Docker / VPS 网络及 0.4.0 真实绘图仍需在目标环境验收，本次文档更新未执行这些验证。历史事实见 [验证记录](validation.md)。
