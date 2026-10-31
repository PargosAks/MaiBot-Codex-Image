# 安装与升级

插件 **0.8.0** 连接已有兼容 MCP **0.5.0**。本仓库不安装、配置或启动 MCP，也不代替 Codex 登录。

## 已有用户：先检查服务入口

升级前由维护者核对旧 MCP 启动命令、工作目录、解释器及可编辑安装所引用的源码路径，确认是否仍依赖本仓库 `service/`。同时确认 MaiBot 实际加载目录及 Host 分配的数据目录，不公开私密配置内容。

**若仍从本仓库 `service/` 运行，不可在原目录 `git pull`、覆盖解压或删除服务文件。** 0.7.0 会移除服务入口；旧进程暂时运行不代表重启安全。

1. 保留旧完整仓库和原路径给服务使用。无需因兼容插件升级重启 MCP。
2. 在独立目录准备新版插件，通过既有插件管理方式使 MaiBot 加载新版。旧仓库若在插件扫描范围内，核对加载路径、避免重复加载；不要擅自移动服务仍引用的旧目录。
3. 保持 Host 插件身份 `pargosaks.maibot-codex-image`、原 `ctx.paths.data_dir` 和其中的 `delivery.sqlite3`。保留原插件 config、URL、secret、允许用户、启用状态与权限；不要通过新建 Host 数据目录切断发送记录。
4. 保留服务配置、登录目录、完整 `state` 及产物。不为插件升级另起服务，**同一 `state` 不得同时由两个服务运行**。数据库备份遵循原系统一致性备份方式，不在运行中随意复制单个数据库文件。
5. 保存并重载插件。先诊断和查询原任务，再按授权范围验证原图获取与发送；不通过重复绘图验证升级。

若服务已经独立于插件仓库，且确认不引用将删除的文件，可在检查本地改动后仅更新插件并重载，保留原配置和数据。无法确认入口时，先完成核查，不原地更新。

本仓库不提供自动迁移服务入口的工具。旧资料参考：[固定提交的 service](https://github.com/PargosAks/MaiBot-Codex-Image/tree/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272/service)、[历史部署文档](https://github.com/PargosAks/MaiBot-Codex-Image/blob/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272/docs/host-service.md)。

## 新用户：先有兼容 MCP，再安装插件

前置条件为 MaiBot 1.3.x、SDK 2.16.x / 2.17.x 和已有的兼容 MCP 0.5.0。向服务维护者取得从 MaiBot 运行环境可达的 URL 与共享密钥，确认服务已准备 Codex 环境和手动登录。本指南不创建新的 MCP 仓库或部署方案。

将源码或插件 zip 内容放入 MaiBot 实际加载的 `plugins/codex_image`，根层包含 `_manifest.json`、`plugin.py`、`bridge.py`、`delivery.py`。不要覆盖已有目录。Docker 使用现有插件挂载对应的宿主机路径，无需向容器安装 Codex 或挂载宿主机登录目录。

在 MaiBot 插件面板配置：

- `connection.url`：从 MaiBot 进程或容器可达的实际 MCP 入口，通常以 `/mcp` 结尾。
- `connection.secret`：与原服务一致、至少 32 字符的共享密钥；不要换新值覆盖服务原密钥。
- `connection.allowed_users`：允许用户的真实 `platform:user_id`，使用英文冒号；默认 `[]` 拒绝所有用户。

另外授予用户 `codex_image_command` 命令权限及插件 `send.text` / `send.image` 能力。名单和 Host 权限独立生效。保存后在 MaiBot 插件管理中重载插件；重载不启停 MCP。

配置示例仅为占位，不能作为实际密钥使用：

```toml
[plugin]
config_version = "0.8.0"
enabled = true

[connection]
url = "http://127.0.0.1:8765/mcp"
secret = "REPLACE_WITH_EXISTING_MCP_SHARED_SECRET"
allowed_users = []
poll_seconds = 2
watch_seconds = 360
```

## Docker 地址

bridge 网络容器内的 `localhost` / `127.0.0.1` 指向容器自身。宿主机服务须使用当前网络实际可达的宿主机地址；`host.docker.internal` 是否可用取决于部署环境，网关地址也需核实。

服务监听、防火墙、容器路由和 MCP Host 校验必须匹配。宿主机可达只证明宿主机链路，不能替代容器内验证。不要通过关闭鉴权或无保护暴露公网解决连接问题。

## 分层验证

先由已授权用户执行 `/画图诊断`，再查 `/画图状态 原任务编号`。原图发送受归属与发送记录限制，`sending/send_unknown` 不可重发。

新用户没有旧任务时，真实 `/画图 描述` 会消耗额度，只在允许真实绘图的验证范围内提交一次，分别核对任务、发送记录与聊天端实际收图。诊断成功不证明登录、绘图或适配器发送成功。本轮状态见 [验证记录](validation.md)，历史通过结论不沿用。

自主创作默认关闭；如需了解持续委托、预算、Host 能力、模型任务和失败恢复，见 [阶段 6](implementation-stage-6.md)。MCP 0.5.0 的命令兼容性不等于具备自主创作所需的 `lookup_v1` / `edit_v1`。
