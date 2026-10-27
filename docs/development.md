# 开发与验证

## 0.5.0 架构与边界

根目录为 MaiBot 薄插件：`plugin.py` 处理命令与宿主身份，`bridge.py` 连接独立 MCP，`delivery.py` 保存发送状态。`service/` 是宿主机独立项目，负责原生 Codex 认证、隔离绘图、任务和图片。

插件配置仅包含 `plugin.enabled/config_version` 及 `connection.url/secret/allowed_users/poll_seconds/watch_seconds`。不再提供 `service.mode` 或插件 `codex.*`，不通过插件生命周期启动内置 MCP，不要求修改 MaiBot 本体、Dockerfile、Compose 或镜像。

本版代码与配置版本均为 0.5.0，尚无 Release。`connection.allowed_users` 是唯一用户白名单，由插件控制谁可发请求；安装工具的 `--allowed-user` 只写入插件配置。服务移除白名单，读取旧 `service.toml` 的 `allowed_users` 时警告并忽略，建议删除旧项。服务仍校验 Bearer、HMAC 签名、有效期、scope 完整性与任务归属，持钥调用方视为可信。

宿主机使用现有服务配置格式、手动 Codex 登录与 `CODEX_IMAGE_SECRET` 环境变量。入口已定义在 `service/pyproject.toml`：

```sh
uv sync --project service --locked --no-dev
uv run --project service --locked --no-dev codex-image-mcp --config /绝对路径/service.toml
```

执行前按 [Linux / Docker](host-service.md) 或 [Windows](manual-setup.md) 指南准备配置与密钥。插件不会自动启动此命令。修改服务参数时保留已有 `home`、`state` 和密钥，不将运行数据放进发布包。

## 本地开发检查

本地回归使用以下命令：

```sh
uv sync --project service
uv run --project service pytest service/tests -q
```

测试环境依赖声明与锁文件见 `service/pyproject.toml`、`service/uv.lock`，开发 SDK 固定上游提交。回归应覆盖真实 MCP HTTP 握手、模拟 Codex 子进程和 MaiBot SDK 配置；运行结果由本版验证记录给出，不沿用旧测试结论。单次真实订阅绘图另行记录，不将模拟测试当作订阅绘图证据。

建议重点核对：

- 插件加载、重载和卸载只管理自身连接与等待任务，不启动 MCP 或 Codex；空白密钥、空白名单、白名单外用户及不可信身份被插件拒绝。
- 服务不再按用户白名单拒绝持钥调用方；仍拒绝错误 Bearer、错误 HMAC、过期请求、不完整 scope 和跨归属任务访问；旧服务 `allowed_users` 警告并忽略。
- 独立 MCP 的 Host 校验、幂等、队列、断线与图片完整性；结果未知时不自动重画。
- 插件连接独立服务后的提交、查询、取图、发送回执与发送未知保护。
- 旧配置迁移不会复制、覆盖或删除原任务与发送数据；已有外部部署保留服务配置、登录目录和密钥。

## 部署和构建工具

首次安装主路径见 [快速指南](quickstart.md)：`scripts/setup_host.py` 在 uv 准备环境后配对服务和插件，不修改 MaiBot 授权；`--systemd` 仅支持 Linux root、系统级 Docker。`install.sh`、`install.ps1`、`deploy.py` 及 `scripts/manage.py` 保留为原有高级可选工具，行为需分别核对。工具修正必须同步核对复制名单与生成配置，不能沿用旧内置行为。

```sh
python scripts/build_bundle.py
```

这是构建入口；执行前应审查构建器白名单。0.5.0 的完整部署包应包含独立 `service/`，插件包只包含插件运行文件与相关文档，不携带内置 MCP 执行组件。不得打包运行数据、密钥、登录、缓存、虚拟环境、`.git` 或本地会话。

本版需分别检查版本元数据、配置备份、重复配置保留任务与登录、挂载与路径越界拒绝；新 `setup_host.py` 还需确认不修改 MaiBot 授权，不能沿用原有工具的授权写入行为。完整包应包含宿主机服务；插件包不包含 `service/`、内置启动器或 Codex 登录工具。

## 验证范围

自动测试、独立审查、目标机部署、浏览器或聊天适配器发图、用户验收分别记录。Docker / VPS 需在目标网络验证 Gateway 绑定和请求 Host；未认证 `401` 不能代替带签名的 MCP 调用。真实绘图须单独记录客户端版本、任务结果和聊天图片，不沿用 0.2.0 / 0.3.0 的通过结论。

历史事实见 [验证记录](validation.md)，其中旧版本结果保持原版本语境，不作为 0.5.0 的通过证据。本版实际 Linux + Docker 安装尚未验收，最终验证另行记录；原生 MCP 的 SDK 边界见 [原生 MCP 验证](native-mcp-feasibility.md)。
