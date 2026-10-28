# 开发与验证

## 0.6.2 架构与版本边界

根目录为 MaiBot 薄插件：`plugin.py` 处理命令与真实宿主身份，`bridge.py` 连接独立 MCP，`delivery.py` 保存发送状态。`service/` 为宿主机独立项目，负责 Codex 认证、隔离绘图、队列、任务和图片。

本次插件与安装工具为 **0.6.2**，服务源码、`service/pyproject.toml`、`service/uv.lock` **保持 0.5.0 不变**，保留现有协议兼容性。不要为统一版本号重写服务元数据或锁文件。尚未发布 Release；测试、审查及目标机验收分别见 [验证记录](validation.md)，不能由设计约定推导为通过。

源码默认只有一份，位于真实 MaiBot `plugins/codex_image`。宿主机从该源码运行独立 MCP；Docker 使用已有插件挂载的宿主机 Source，不增加挂载，不修改 MaiBot、Compose、Dockerfile、镜像或防火墙。

插件配置仅包含 `plugin.enabled/config_version` 和 `connection.url/secret/allowed_users/poll_seconds/watch_seconds`。不提供 `service.mode` 或插件 `codex.*`，不随插件加载、重载或卸载启停 MCP。`connection.allowed_users` 控制用户请求，服务继续校验 Bearer、HMAC、时效、scope 与任务归属；旧服务白名单警告并忽略，持钥调用方视为可信。

## 安装工具约定

`setup.sh` 是新用户入口，需要现成 Git、uv 及手动完成的宿主机 Codex 登录。使用 uv 隔离环境运行配置工具，再准备服务环境；不得代替 Codex 登录或 MaiBot 授权。

- 默认插件路径为脚本所在源码根目录，`--plugin-dir` 保留旧分离布局兼容，不再强制分离两份源码。
- 容器名可交互填写，直接部署留空，多网络需选择。支持 `--container`、`--network`；从不询问或强制 `--allowed-user`。
- 新安装默认白名单 `[]`，插件拒绝所有用户；配置 MCP 后由用户在 WebUI 设置真实 `platform:user_id` 和 MaiBot 权限，保存并重载。旧 `--allowed-user` 仅作为可选的首次预填入口保留，可重复。
- 已有 `connection.allowed_users` 不校验、改写或清空；包括空值或曾填错格式，不得因此阻塞安装，也不得被可选预填参数覆盖。名单由用户在 WebUI 修正和维护。
- Linux root 默认 systemd，可显式 `--systemd`；`--foreground` 选择前台模式。非 root 的后台管理、user namespace 和特殊 Docker 挂载继续手动处理。
- 新单目录数据默认 Linux root `/var/lib/maibot-codex-image`，其他用户 `${XDG_DATA_HOME:-$HOME/.local/share}/maibot-codex-image`，支持 `--data-dir`。
- 数据目录保存 `service.toml`、`secret.txt`、`runtime/state` 和 `venv`；插件 `config.toml` 仍在源码根目录并被 Git 忽略，发送记录继续使用 Host `ctx.paths.data_dir`。
- 原 Codex 登录不复制；已有配置、密钥和 `state` 路径优先保留，不能为了采用新默认值而搬库或轮换密钥。

root 后台执行安全检查必须保留：源码与父目录不可被其他宿主机用户替换。插件配置可能需由容器 UID 读取，其属主不能简单套用到可执行源码；应区分配置和代码，但不能取消源码检查。保持既有挂载遮蔽检查，宿主机私有密钥和 Codex 凭据不能放进容器可见源码目录。

旧手工 systemd 单元不能自动覆盖或宣称已迁移。配置冲突、缺失原密钥应明确失败，不删除文件绕过。更改服务配置、启动单元或首次准备服务环境需先停止服务；插件侧更新无需停止。

当服务配置、启动单元和 `venv` 均无需变化时，允许运行中的 MCP 进行插件配置版本更新或重复配对；此路径不取得 `StateLease`，不写入 `secret.txt` 或 `service.toml`。`--prepare-environment` 在服务解释器缺失或初装尚未生成服务配置时执行 uv 同步；已配对且环境存在时不改写运行中的服务环境。安装器的预检、备份及两条路径的行为需分别验证。

root 权限校验仅对明确传入的环境根目录 `.lock` 区分元数据用途，要求普通文件、root 属主、无执行位。不可按后缀跳过其他 `.lock`，不可跳过解释器、依赖或环境目录检查。uv 同步子进程清除配置工具继承的 `VIRTUAL_ENV`，保留原调用环境，目标由 `UV_PROJECT_ENVIRONMENT` 明确指定。

初装失败留下的空状态目录和唯一空普通 `.service.lock` 不视为旧任务；仍需在后续配置写入前取得服务 lease。任何数据库、产物、未知文件、非空锁或链接均视为已有状态。已有配置、配对记录或插件连接缺失原密钥时仍拒绝自动换新。

## 插件升级无需重启 MCP 的适用范围

仅插件变动且协议兼容时，`git pull --ff-only` 后在 WebUI 重载插件即可，原 MCP 进程继续工作。本次保留服务 0.5.0，不能把插件版本提升当成服务重启理由。

源码更新不会自动替换运行中的服务代码。若改变 MCP 源码、依赖、监听或密钥，需要另行停止服务，核对原登录、配置、密钥和完整任务路径，再重新配置或准备环境并启动。不得承诺服务热更新，也不通过改写运行中虚拟环境实现升级。

已有分离部署可只更新插件，继续访问原 URL、密钥和 `state`，不运行新安装器。旧宿主机仓库可能仍是服务入口，不自动删除。确需改用新源码启动时，停止服务核对后使用 `--data-dir 原配置目录`，保留旧 `state`；后台单元迁移另行处理。0.2 / 0.3 内置服务不适用此兼容路径，保留旧任务与发送记录，见 [迁移说明](advanced.md#从旧版本迁移)。

## 本地开发检查

从源码目录准备开发环境并运行回归：

```sh
uv sync --project service
uv run --project service pytest service/tests -q
```

这用于开发检查，不是新用户部署的前置命令。依赖与锁文件见 `service/pyproject.toml`、`service/uv.lock`；不为本次安装布局调整升级服务依赖。

回归重点覆盖以下行为，实际结果与环境见验证记录：

- 单目录安装及旧分离布局、默认数据路径和 `--data-dir`、systemd 默认与显式前台、无交互参数。
- 重复配置保留密钥、登录和完整任务路径；服务配置、单元和环境不变时，运行中服务允许插件侧更新，且不取得任务锁或写服务配置、密钥。
- 更改服务配置、单元或首次准备环境时，运行中服务应拒绝；配置冲突、缺少原密钥明确失败，不覆盖旧手工单元；已有 `venv` 不重复同步。
- Docker Gateway、容器 UID/GID、可读插件配置、源码父目录权限、嵌套挂载和路径遮蔽拒绝。
- 插件重载不启停 MCP；协议兼容的插件更新后仍由同一 MCP 进程查询旧任务、取回原图，重复提交保持幂等；不能只用健康接口证明。
- 空白名单、未授权或不可信身份在插件侧拒绝；服务拒绝错误 Bearer / HMAC、过期 scope 和跨归属访问。
- 安装流程不询问或要求用户白名单；首次默认空名单，可选参数仅首次预填；已有空名单或错误格式原样保留且不阻塞安装。WebUI 修改后保存、重载即可生效，无需重启 MCP。
- 发送未知保护、原任务库和发送记录不被清空，0.2 / 0.3 数据不被误当成已迁移。

运行独立进程与模拟 Codex 的集成测试，可以验证连接、任务与不重启边界；不能替代实际 Codex 绘图或 WebUI 重载验收。

## 构建与验证记录

构建入口仍为：

```sh
python scripts/build_bundle.py
```

执行前审查白名单。完整源码用于单目录安装，包含 `setup.sh`、配置工具和 `service/`；若仍提供薄插件包，它面向已有独立服务，不等于具备宿主机安装入口。不得打包实际配置、密钥、登录、任务、缓存、虚拟环境、`.git` 或本地会话。本轮不发布 Release。

自动测试、独立审查、Docker / VPS 部署、实际聊天发图和用户验收分别记录。协议兼容与同一进程复用须绑定版本及环境；模拟 Docker / systemd 不等于真实部署通过。真实绘图另行记录客户端版本、任务状态和聊天收图结果。

[验证记录](validation.md) 中的旧版本结果保持历史语境，不作为 0.6.2 安装流程通过证据；原生 MCP 的 SDK 边界见 [原生 MCP 验证](native-mcp-feasibility.md)。
