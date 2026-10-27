# 进阶配置

## 内置服务

`service.mode` 可选 auto、embedded、external。auto 在共享密钥为空时使用内置服务，非空时兼容原外部服务；embedded 强制使用内置服务，external 强制使用外部地址和密钥。新用户保留默认即可。

内置服务使用插件 Runner 的事件循环，不创建第二个 MCP 进程、不安装进程信号处理器。监听 socket 预先绑定 127.0.0.1 的随机端口，并保持到服务器接管。加载和配置更新有串行锁；配置更新先停止旧服务，再使用新白名单启动。卸载会停止监听、取消本插件的取图任务并中断自己创建的 Codex 进程。

内部共享密钥只在首次启动创建，后续保留；同一数据目录使用操作系统文件锁拒绝重复实例。插件源码更新不会替换任务和发送数据库。运行数据默认位于 `data/plugins/pargosaks.maibot-codex-image`，以实际 Host 分配路径为准。内置任务在 embedded/state，发送日志仍为 delivery.sqlite3。

专用 Codex 登录目录默认 `~/.codex-maibot-image`，可通过 codex.home 配置。需由运行 MaiBot 的同一用户手动登录。切换外部模式或登录目录不会自动搬迁历史任务、图片或凭证。

## 外部服务

服务配置中的 binary 指向 Codex 原生程序，home 是本服务专用登录目录，state 保存 SQLite 任务与图片。两端的随机共享密钥、允许用户和访问地址必须匹配。

Codex 专用 home 不应含 config.toml，以免加载其他模型 Provider、MCP 和工具。登录命令的 cli_auth_credentials_store 参数只作用于当前调用，不需要保存到配置文件。

MCP 默认只监听 127.0.0.1。跨容器或跨机器部署时，需要设置受限内网和正确的 host、插件 url；服务的 MCP Host 检查需要访问地址与监听配置一致。跨不可信网络应配置 TLS，密钥不能放在 URL 查询参数中。

部署工具的命令授权只增加 plugin.command_permissions 中本绘图命令的 allow_users，不授予全局操作员权限。no-grant-command 可关闭实例配置改写。

任务使用稳定请求键去重；相同请求键但不同描述会拒绝。图片完成与回合完成分别记录；断线后不会自动重复生成。发送结果未知继续锁定，明确的宿主能力拒绝才视为已知发送失败。

暂不自动清理图片。停止服务后备份服务状态和 MaiBot 实例中的插件数据；不要删除 SQLite 来强制重试。
