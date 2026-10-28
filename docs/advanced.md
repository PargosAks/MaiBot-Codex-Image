# 进阶配置与迁移

首次安装使用 [快速开始](quickstart.md) 的 `bash setup.sh`。源码直接放在真实 `plugins/codex_image` 中，插件与安装工具为 **0.6.3**；独立 MCP 服务保持 **0.5.0**，协议兼容。脚本不替人登录 Codex 或授予 MaiBot 权限。各版本验证范围见 [验证记录](validation.md)，尚未发布 Release。

## 单份源码与数据目录

新安装默认插件目录为脚本所在源码根目录，不需要准备第二份宿主机仓库。宿主机从同一份 `service/` 源码运行独立 MCP；MaiBot 插件只提交、查询和取图，不管理服务生命周期。

| 内容 | 保存位置 |
| --- | --- |
| 源码 | MaiBot 实际加载的 `plugins/codex_image` |
| 插件配置 | 源码根目录 `config.toml`，Git 忽略 |
| 服务配置、共享密钥 | 宿主机数据目录 `service.toml`、`secret.txt` |
| 新任务库、图片、隔离绘图目录 | 新数据目录的 `runtime/state`，由 `service.toml` 的 `state` 指定 |
| 服务虚拟环境 | 数据目录 `venv` |
| Codex 登录 | 服务用户原有 `CODEX_HOME` 或 `~/.codex`，不复制 |
| 插件发送记录 | Host 提供的 `ctx.paths.data_dir`，保持原位置 |

新单目录安装的数据目录规则：

- Linux root：`/var/lib/maibot-codex-image`。
- 其他用户：`${XDG_DATA_HOME:-$HOME/.local/share}/maibot-codex-image`。
- 用 `--data-dir /原配置目录` 可显式指定；已有配置的 `state` 路径优先保留，不因默认值而迁移。

`setup.sh` 需要已有 uv、Git 和手动完成的宿主机 Codex 登录，使用 uv 的隔离环境运行配置工具，再准备服务环境。Linux root 默认配置 systemd；`--foreground` 选择前台，`--systemd` 显式选择后台，两者互斥。`--plugin-dir` 只为旧分离布局保留，新用户无需填写。容器与网络参数见 [参数示例](quickstart.md#按参数安装)。

脚本不询问或强制填写允许用户。新安装默认 `connection.allowed_users = []`，拒绝所有用户；配置 MCP 后，在 MaiBot WebUI 插件面板填写真实 `platform:user_id`，授予 MaiBot 命令与发图权限，保存并重载后才能绘图。已有名单不校验、改写或清空，空名单或错误格式不阻塞安装；请在 WebUI 修正。旧 `--allowed-user` 仅作为可选的首次预填入口保留，可重复，不覆盖已有名单。

服务配置、启动单元和环境均无需变化时，允许在 MCP 运行期间更新插件配置版本或重复配对，不写入原服务配置和密钥。只有更改服务配置、启动单元或首次准备服务环境时才需先停止服务。`--prepare-environment` 在服务解释器缺失或初装尚未生成服务配置时同步；已配对且环境存在的重复运行不更新服务环境，也不代表服务依赖已升级。

uv 的 Unix 实现会主动将环境协调锁设为 `0666`，不能把它等同于可写源码。安装器只对指定 `venv/.lock` 的普通、root 所属、无执行位文件区分处理；符号链接、其他同名文件及可写源码/依赖仍拒绝。官方实现见 [uv 锁文件源码](https://github.com/astral-sh/uv/blob/329bf9448afd9f9088d14a4d57fbc61eb454f117/crates/uv-fs/src/locked_file.rs#L298-L338)。初装中断重试见 [快速指南](quickstart.md#安装中断后重试)。

Docker 场景源码放在既有插件挂载对应的宿主机 Source 内。宿主机数据和登录不放进该挂载。工具验证现有挂载及遮蔽、容器用户和网络；不修改挂载、Compose、镜像、防火墙或 MaiBot 本体。root 后台服务仍拒绝可被其他宿主机用户替换的源码和父目录。容器可写源码时，也进入了宿主机服务代码的信任边界；不能靠隐藏目录或随意放宽权限解决。

非 root 的后台管理、rootless / user namespace Docker、特殊挂载和自定义运行身份继续手动处理，详见 [宿主机指南](host-service.md)。本版不会自动迁移已有 systemd 单元。

## 日常升级与服务维护

只要更新限于插件并保持 MCP 协议兼容，执行 `git pull --ff-only` 后在 WebUI 重载插件即可。原独立 MCP 进程继续接收任务，URL、密钥和任务数据保持原样；不要为此重新运行配置工具。

MCP 服务虽是中间层，仍负责 Codex 认证、队列、任务库与图片，它的源码和依赖更新不会自动生效。更新涉及 MCP 源码、依赖、监听地址或密钥时，需要单独维护：

1. 停止提交新任务，等待在途任务处理完，记录未确认结果。
2. 停止原服务，核对并备份原配置、密钥、完整 `state` 和插件发送记录；确认实际服务入口及 Codex 登录目录。
3. 保持原 `home`、`state` 和密钥，按变更需要重新配置或准备环境，再从核对后的入口启动服务。
4. 使用原任务编号检查状态和图片；不通过重复提交来补图。

不要在服务运行时更换其环境或配置。服务不支持源码热更新；遇到未知结果也不自动重画。本次 0.6.3 保持服务源码、`service/pyproject.toml` 和 `service/uv.lock` 为 0.5.0，因此兼容的既有 0.5.0 服务不需要重启。

## 宿主机服务配置

服务仍使用 `codex-image-mcp --config 配置绝对路径` 读取 `service.toml`。共享密钥至少 32 字符，从 `CODEX_IMAGE_SECRET` 或 `CODEX_IMAGE_SECRET_FILE` 读取；直接密钥变量存在时优先，空值也不回退到文件。密钥不放进 TOML 或 URL。

| 服务字段 | 用途 |
| --- | --- |
| `binary` | 省略或留空自动查找原生 Codex，也可指定程序路径 |
| `home` | 省略或留空复用 `CODEX_HOME` 或 `~/.codex`，显式路径优先 |
| `state` | 必填，任务、图片与隔离绘图的持久化绝对路径 |
| `host` / `port` | 默认 `127.0.0.1` / `8765`，容器场景匹配实际访问地址 |
| `model` | 默认空，使用隔离绘图客户端默认模型 |
| `timeout` / `rpc_timeout` | 默认 300 / 30 秒，绘图等待与 RPC 超时 |
| `max_queue` | 默认 10，待处理队列上限 |
| `max_image_bytes` | 默认 20971520，单图字节上限 |

字段模板见 [服务模板](../service/config.example.toml)。新安装由脚本配对；已有配置不要重新生成。0.5.0 服务已移除用户白名单，旧 `allowed_users` 读取时警告并忽略；允许用户只由插件 `connection.allowed_users` 控制。

## 登录复用与绘图隔离

`home` 省略或留空时使用服务进程的 `CODEX_HOME`，未设置时使用服务用户的 `~/.codex`；显式路径优先，TOML 不展开环境变量文本。服务需沿用原用户与原登录目录；专用目录如 `~/.codex-maibot-image` 可显式继续使用。

原生认证进程使用该目录读取账号并刷新访问令牌，不创建绘图线程。凭证仍由 Codex 原有文件或 keyring 后端管理；服务不复制 `auth.json`，不写回日常 `config.toml`。Codex 本身可能按既有机制刷新凭证，保留登录并非禁止正常刷新。

每个任务的隔离绘图进程使用 `state/codex-runtime/任务编号`，短期令牌通过本机标准输入传递，采用实验性 `chatgptAuthTokens` 和 ephemeral 凭证存储，不写进命令行、日志或配置。账号变化时拒绝继续，不自动换号重画。

绘图进程显式使用 OpenAI Provider，关闭钩子、插件、Apps、Shell、网页搜索和项目文档注入；系统或组织管理要求仍由 Codex 执行。隔离运行目录不应生成 `auth.json`，图片与缓存可落盘。

客户端需支持 `getAuthStatus` 和实验性 `chatgptAuthTokens`，历史兼容检查见 [验证记录](validation.md)，不能推断任意版本都可用。本服务要求 ChatGPT 登录，不使用 API Key 代替。

## 网络与权限

插件仅配置 `plugin.enabled/config_version` 及 `connection.url/secret/allowed_users/poll_seconds/watch_seconds`。插件白名单由用户在 MaiBot WebUI 维护，控制谁可发请求，还需 MaiBot 授予对应用户命令权限及插件 `send.text` / `send.image` 能力。修改后保存并重载插件，无需重启 MCP。

服务继续校验 Bearer、HMAC、有效期、scope 完整性及任务归属。持有共享密钥的调用方视为可信，不应将密钥交给模型或不可信调用方。

Docker bridge 使用现有网络的宿主机 Gateway，监听和插件 URL 匹配，`0.0.0.0` 不是请求 Host 的通配许可。容器通过 MCP 取图，无需挂载宿主机 Codex 凭据或图片目录。跨不可信网络需另行配置 TLS 并核对代理 Host，该场景不是已完成的部署验证。

MaiBot 原生 MCP 不能直接代替当前插件的身份签名、轮询和定向发送，被检 SDK 边界见 [原生 MCP 验证](native-mcp-feasibility.md)。

## 从旧版本迁移

### 已使用独立 MCP 的 0.4.x / 0.5.x 用户

已有服务协议兼容时，**只更新插件源码并在 WebUI 重载**，继续使用原 URL、密钥、`home`、`state` 和发送记录。不需要运行新 `setup.sh`，不需要迁移任务，也不需要把宿主机服务仓库搬进插件目录。0.4.x 服务应先核对其协议与配置差异；本次明确保持的是 0.5.0 服务兼容性。

**不要删除旧宿主机仓库。** 正在运行的进程及 systemd 的 `WorkingDirectory`、`ExecStart` 可能仍引用它。单份源码是新安装的默认布局，不代表旧服务入口已经自动迁移。

若确实希望以后从新插件目录启动服务：先停止原服务并核对其实际单元、配置和完整任务路径，再将 `--data-dir` 指向原来包含 `service.toml`、`secret.txt` 的宿主机配置目录，例如：

```sh
bash setup.sh --data-dir /原宿主机配置目录 --foreground
```

此处必须替换为真实路径，不能只因旧仓库位于某目录就认定它是配置目录。工具应沿用原 `service.toml` 的 `state`，不搬迁数据、不轮换密钥、不复制登录；发现配置不一致或原密钥缺失时停止核查。旧手工 systemd 单元不会被自动覆盖，后台入口需人工核对处理；`--foreground` 示例也不迁移旧单元。禁止新旧实例同时打开同一任务目录。

### 原来使用内置服务的 0.2.x / 0.3.x 用户

当前插件不会再自动启动内置服务；旧 `service.mode` 和插件 `codex.*` 不再适用，旧 `login.py` 也已移除。

1. 先停止提交新任务，尽可能在旧环境查询并取回结果，记录未确认任务编号。
2. 停止旧插件及内置服务后，备份原插件配置和 Host 分配的整个插件数据目录。常见位置为 `data/plugins/pargosaks.maibot-codex-image`，以实际路径为准；保留 `embedded/state`、旧密钥和 `delivery.sqlite3`。
3. 按快速指南准备宿主机独立服务，手动确认宿主机已有 Codex 登录；容器内登录不会自动变为宿主机登录。
4. 配对新的服务连接，在插件设置允许用户并由你完成 MaiBot 权限配置，重载后先诊断再按需进行一次绘图验收。

**不自动导入、搬迁或删除旧内置任务。** 新服务默认有自己的任务库，旧任务未找到不代表被删除。产物记录含文件路径，不能只复制数据库就认定迁移完成。

保留完整旧版本与旧环境供回滚核查，不让新旧服务同时使用同一任务目录，不将新服务直接指向旧内置目录试探迁移。确需导入，应另行核对路径、任务归属和发送记录。

## 任务与数据保留

稳定请求键用于去重，同键不同描述会被拒绝。图片完成与回合完成分别记录；服务重启时，原 `starting/running` 根据产物转为 `artifact_ready` 或 `outcome_unknown`，不代表自动续跑。

自动等待超时后仍可用原编号查询和取图。发送未知继续锁定，应先检查聊天记录，不删除发送记录强行重试。插件发送记录继续使用 Host 原数据目录，不搬到源码根目录。

服务暂不自动清理图片。备份应在停止相应服务及插件后，保留宿主机完整 `state`、配置和密钥，以及插件完整数据目录；不要只备份 SQLite。原任务产物路径必须保持，不能只搬数据库或为了新默认路径改 `state`。

## 高级可选工具

普通用户使用 [快速指南](quickstart.md) 的 `setup.sh`。`install.sh`、`install.ps1`、`deploy.py` 和管理脚本保留为旧高级工具，行为应分别核对，不能当作本版三条命令的必要步骤。

旧工具可能下载 Codex、复制薄插件或按参数修改命令授权，并不提供新脚本相同的 Docker Gateway 与挂载验证。不要混用工具去覆盖已有配对。本版和历史测试范围分别记录，不将旧工具测试视为新流程通过。
