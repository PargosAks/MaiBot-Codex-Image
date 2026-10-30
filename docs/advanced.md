# 配置与迁移

## 插件与服务边界

插件 **0.7.0** 兼容独立 MCP **0.5.0**，保留协议、配置字段与发送库。插件提交、查询和取图发送，服务负责 Codex、登录、队列、任务、产物与 `state`。本仓库不提供新的独立 MCP 仓库地址或服务安装器。

配置包含 `plugin.enabled/config_version` 和 `connection.url/secret/allowed_users/poll_seconds/watch_seconds`，不增加 `service.mode` 或插件 `codex.*`。白名单由插件面板维护，不要求服务另建名单；连接鉴权与任务归属校验继续生效。

## 配置约束

- URL 允许 HTTP / HTTPS，不能带用户名、密码、查询参数或 fragment；使用实际 MCP 入口，不将 secret 放入 URL。
- secret 至少 32 字符且与服务一致。Bridge 使用 Bearer 和签名，不跟随重定向，也不继承环境代理；部署方须提供直接可达的入口。
- allowed_users 使用真实 `platform:user_id`，空列表拒绝所有人；Host 命令及 `send.text` / `send.image` 能力另行授权。
- poll_seconds 默认 2 秒，范围 0.1–30；watch_seconds 默认 360 秒，范围 1–3600。等待结束只停止自动取图，不取消或重提服务任务。

Docker bridge 的 localhost 属于容器自身。核对实际 URL、监听、路由、防火墙及 MCP Host 校验，只保留必要且受保护的访问链路。服务凭据和任务目录不进入插件挂载。

## 从旧版本迁移

升级前核对旧 MCP 启动命令、工作目录、解释器及可编辑安装源码引用。**若仍引用本仓库 `service/`，不得在旧目录原地 `git pull` 或覆盖安装 0.7.0。** 删除源码可能破坏后续加载及重启，进程尚存不能证明安全。

保留旧完整仓库给服务，插件部署到独立目录。切换加载位置时保留 Host 插件身份 `pargosaks.maibot-codex-image` 和原 `ctx.paths.data_dir`，避免重复加载同一插件；不要移动服务仍引用的旧目录。

| 必须保留 | 原因 |
| --- | --- |
| Host 身份、原 `data_dir`、`delivery.sqlite3` | 发送意图、结果及未知发送保护 |
| 插件 config、名单、启用状态和权限 | 保留连接及授权 |
| 原 URL、secret、服务配置与 Codex 登录目录 | 继续访问原服务，不自动换密钥或复制凭据 |
| 完整服务 `state` 及产物 | 保留任务、归属与幂等，不只搬图片或单个数据库 |
| 旧完整仓库及原运行环境 | 保留服务入口与恢复方式 |

同一 `state` 只运行一个服务实例。插件升级无需启动第二个 MCP；确需服务迁移时，另行安排停机及一致性备份，确认原实例停止并释放目录后才可启动替代实例。不删锁、清库或重建密钥绕过冲突。

旧 0.2 / 0.3 内置任务不因填写新 URL 自动迁移。保留旧数据与发送记录，由维护者确认兼容及迁移方案，不宣称自动导入或安全重放。

具体插件步骤见 [quickstart.md](quickstart.md)。旧服务维护参考 [固定提交 service](https://github.com/PargosAks/MaiBot-Codex-Image/tree/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272/service) 与 [历史部署指南](https://github.com/PargosAks/MaiBot-Codex-Image/blob/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272/docs/host-service.md)，不作为当前可执行安装步骤。

## 发送记录与未知结果

发送前写入 `sending`，加载时遗留 `sending` 转为 `send_unknown`，两者均不能自动或手动重发。`sent/send_failed` 仅在手动路径按原规则发送原图，不创建新生成。

绘图超时、`outcome_unknown` 或发送未知时，先查原任务与实际聊天记录，由维护者核对；不删发送库或换任务编号绕过保护。健康检查、任务完成、发送确认与聊天收图分别验证。
