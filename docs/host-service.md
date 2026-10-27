# 宿主机 Codex 部署指南（Linux VPS）

适用于：MaiBot 在 Docker 内或直接部署，但 Codex 始终安装并登录在宿主机。

```text
MaiBot 插件（容器内或宿主机） → 宿主机 MCP 服务 → 宿主机 Codex
```

这是已有 external 模式的部署方式。无需修改 MaiBot 本体、Dockerfile、镜像或 Compose 配置。容器只需标准插件及 Python 依赖；登录凭证和图片保存在宿主机，通过 MCP 返回图片。

宿主机 MCP 需要单独启动并保持运行；容器内的插件不会直接启动宿主机程序。

## 1. 在宿主机准备服务

宿主机需要 Git、uv、Python 3.12+ 和已安装的官方 Codex CLI。以下命令均在宿主机执行，使用已登录 Codex 的系统用户：

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git /opt/maibot-codex-image-host
cd /opt/maibot-codex-image-host
uv sync --project service --locked --no-dev --python 3.13
python3 login.py --status
```

已有 ChatGPT 登录无需重登；没有登录时手动执行 `codex login --device-auth`。MCP 服务用户应与宿主机登录用户一致，与容器内用户无关。目录可按该用户的写入权限调整。

## 2. 选择容器可达的宿主机地址

常见 Linux Docker Engine bridge 网络可使用容器所连网络的宿主机网桥网关地址。先只读查看，maibot 替换为实际容器名：

```sh
docker inspect --type=container maibot --format '{{json .NetworkSettings.Networks}}'
```

从结果中找所属网络的 Gateway，例如 **172.18.0.1**。多个网络时选用实际要连接的那一个，不要直接照抄示例 IP。

宿主机 MCP 监听这个地址，插件 URL 也使用同一地址。无需新增端口映射或 host.docker.internal 映射；不要把容器内 127.0.0.1 当作宿主机。

若 MaiBot 仅直接部署，或已有容器使用 host 网络，可使用 127.0.0.1。Docker Desktop、rootless、无网桥地址或受限网络，需要另行确认实际可达的宿主机地址；本例不代表已验证这些环境。

## 3. 配置宿主机服务

在仓库根目录创建 service.toml，替换 binary、home 和 host：

```toml
binary = "/实际路径/codex"
home = "~/.codex"
state = "/opt/maibot-codex-image-host/runtime/state"
allowed_users = ["qq:123456789"]
host = "172.18.0.1"
port = 8765
model = ""
```

binary 必须是原生程序路径，可在此目录执行 `python3 -c 'from client import find_codex; print(find_codex())'` 查询。home 填宿主机实际登录目录；若使用自定义 CODEX_HOME，请填对应路径。

只监听所需的宿主机网桥地址。当前服务会校验 HTTP Host，因此插件访问地址应与 host 一致，不要把 host 改成 0.0.0.0 后仍照用本示例 URL。宿主机防火墙需允许实际 Docker 子网访问此端口，无需开放公网访问。

首次生成共享密钥，保存为被 Git 忽略的 secret.txt；以后启动复用同一文件：

```sh
umask 077
if [ ! -f secret.txt ]; then
  ./service/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(40))' > secret.txt
fi
export CODEX_IMAGE_SECRET="$(cat secret.txt)"
./service/.venv/bin/python -m codex_image_mcp.server --config ./service.toml
```

保持此进程运行；正式使用可自行交给进程管理器。MaiBot 或容器重启不会替宿主机启动 MCP。

## 4. 配置 MaiBot 插件

按标准方式将插件安装到 MaiBot 实例的 plugins/codex_image。Docker 部署时，使用 MaiBot 现有的插件安装或持久化目录机制，不需要把宿主机 Codex 目录挂载进容器。

在插件设置中选择 external，并配置以下值；若直接编辑 config.toml，请保留原来的 [plugin] 与 config_version：

```toml
[service]
mode = "external"

[connection]
url = "http://172.18.0.1:8765/mcp"
secret = "填写宿主机 secret.txt 中的完整密钥"
allowed_users = ["qq:123456789"]
```

共享密钥至少 32 字符，两端白名单一致。同时授予对应用户本插件命令和文字、图片发送权限，重新加载插件。external 模式不使用容器内的 codex.binary、codex.home 或其他 codex.* 设置。

直接部署的 MaiBot 也可以使用这份连接配置；如果宿主机服务仅监听 127.0.0.1，则相应将 URL 改为 http://127.0.0.1:8765/mcp。

## 5. 验证

- 宿主机执行 `python3 login.py --status` 确认相应目录的 ChatGPT 登录。
- 在 MaiBot 中执行 `/画图诊断`。当前 external 模式只检查 MCP 连接，登录会在绘图时再次检查。
- 执行 `/画图 一个绿色三角形，白色背景`，核对任务和自动发图。

排查网络时，可利用容器已有 Python 发起一次不带密钥的请求，无需额外安装 curl：

```sh
docker exec maibot python3 -c 'import http.client; c=http.client.HTTPConnection("172.18.0.1",8765,timeout=5); c.request("GET","/mcp"); print(c.getresponse().status)'
```

返回 **401** 说明服务已可达且拒绝未认证请求；这不代表登录或绘图已通过。连接拒绝通常是服务未启动或监听地址不对；超时需检查网络和防火墙；403 需核对 MCP 访问地址与 Host 校验。

本地已有 external 模式的 MCP 传输、鉴权和发图回归验证；本文的 Linux Docker 宿主机网络步骤仍需目标 VPS 实测。本次未修改或启动 Docker 容器。

参考：[Docker bridge 网络](https://docs.docker.com/engine/network/drivers/bridge/)、[docker inspect](https://docs.docker.com/reference/cli/docker/inspect/)。
