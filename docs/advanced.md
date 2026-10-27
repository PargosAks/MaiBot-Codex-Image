# 进阶配置

## 内置服务

`service.mode` 可选 auto、embedded、external。auto 在共享密钥为空时使用内置服务，非空时兼容原外部服务；embedded 强制使用内置服务，external 强制使用外部地址和密钥。新用户保留默认即可。

内置服务使用插件 Runner 的事件循环，不创建第二个 MCP 进程、不安装进程信号处理器。监听 socket 预先绑定 127.0.0.1 的随机端口，并保持到服务器接管。加载和配置更新有串行锁；配置更新先停止旧服务，再使用新白名单启动。卸载会停止监听、取消本插件的取图任务并中断自己创建的 Codex 进程。

内部共享密钥只在首次启动创建，后续保留；同一数据目录使用操作系统文件锁拒绝重复实例。插件源码更新不会替换任务和发送数据库。运行数据默认位于 `data/plugins/pargosaks.maibot-codex-image`，以实际 Host 分配路径为准。内置任务在 embedded/state，发送日志仍为 delivery.sqlite3。

0.3.0 起，codex.home 留空时，优先使用 MaiBot 进程的 CODEX_HOME，否则使用当前用户的 ~/.codex。也可配置专用目录。必须由运行 MaiBot 的同一系统用户登录，容器内外、root 与普通用户的默认登录彼此独立。旧用户可显式保留 ~/.codex-maibot-image；切换目录不会搬迁凭证或历史任务。

## 登录复用与绘图隔离

原生 Codex 认证进程使用所选登录目录，仅读取账号与刷新访问令牌，不创建线程、不发起绘图或调用工具。它沿用原凭证后端（文件或 keyring），插件不读取或复制 auth.json，不写回用户 config.toml。

每个任务另起隔离的绘图进程，CODEX_HOME 位于插件 state/codex-runtime/任务编号；使用官方实验性 chatgptAuthTokens 接口和 ephemeral 凭证存储。短期访问令牌只通过本机标准输入传递，不放进命令行、日志或配置。需要刷新时由认证进程处理；若账号已变化则拒绝继续，不自动切换账号重画。

绘图运行目录没有日常用户配置，显式使用 OpenAI Provider，并关闭钩子、插件、Apps、Shell 工具与网页搜索，关闭项目文档注入。系统或组织管理的要求仍由 Codex 执行。运行缓存与图片可以落盘，但不应生成 auth.json；这些数据保存在被打包白名单排除的运行目录。

本方案需要客户端支持 getAuthStatus 和实验性 chatgptAuthTokens；若认证检查不通过，先检查客户端版本和 ChatGPT 登录。它不接受 API Key 作为订阅绘图认证。

## 外部服务

若希望改用 MaiBot 原生 MCP 配置、移除插件客户端，先看 [原生 MCP 复用验证](native-mcp-feasibility.md)：原生连接兼容，但当前 SDK 未开放插件调用共享连接的接口，现版本不能直接替换。

Docker 内的插件可以连接宿主机 MCP，使用宿主机已登录的 Codex。直接部署的 MaiBot 也能连接同一服务。无需在容器安装 Codex、挂载宿主机凭证、共享图片目录或授予 Docker 控制权限；图片通过 MCP 返回。详见 [Linux VPS 宿主机部署](host-service.md)。

容器中的插件无法自行启动宿主机进程，宿主机服务需要独立运行。此模式使用 service.mode=external，codex.* 不参与宿主机配置；宿主机 home 由实际运行服务的系统用户解析。

服务配置中的 binary 指向 Codex 原生程序，home 选择已有登录目录，state 保存 SQLite 任务与图片。外部模式同样使用认证复用与绘图隔离。两端的随机共享密钥、允许用户和访问地址必须匹配。

可选安装器新部署默认选取现有 CODEX_HOME 或 ~/.codex；更新部署时保留原服务配置中的 home。生成的登录助手会使用这一目录，不强制改成文件存储。

MCP 默认只监听 127.0.0.1。跨容器或跨机器部署时，需要设置受限内网和正确的 host、插件 url；服务的 MCP Host 检查需要访问地址与监听配置一致。跨不可信网络应配置 TLS，密钥不能放在 URL 查询参数中。

部署工具的命令授权只增加 plugin.command_permissions 中本绘图命令的 allow_users，不授予全局操作员权限。no-grant-command 可关闭实例配置改写。

任务使用稳定请求键去重；相同请求键但不同描述会拒绝。图片完成与回合完成分别记录；断线后不会自动重复生成。发送结果未知继续锁定，明确的宿主能力拒绝才视为已知发送失败。

暂不自动清理图片。停止服务后备份服务状态和 MaiBot 实例中的插件数据；不要删除 SQLite 来强制重试。
