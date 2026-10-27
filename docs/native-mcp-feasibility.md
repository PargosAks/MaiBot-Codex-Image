# MaiBot 原生 MCP 复用验证

当前 0.4.0 架构已统一为 **external-only：宿主机独立 MCP + 宿主机 Codex + MaiBot 薄插件**。插件仍保留自己的 MCP 客户端，用于身份签名、任务查询与定向发图；“外部服务”不等于“插件复用 MaiBot 原生 MCP 连接”。部署见 [宿主机指南](host-service.md)。

以下为原验证事实，未因本次文档更新重新执行。原验证日期：2026-10-28。结论由当时两名子代理的 SDK/协议审查与主代理的实际 MCP HTTP 测试共同形成；原验证本身没有迁移插件架构或发布 Release，也不证明 0.4.0 真实绘图或 Docker / VPS 已通过。

## 原验证结论

MaiBot 的原生 MCP 配置可以连接宿主机绘图服务。被检 SDK 未提供插件调用主程序已经建立的 MCP 连接的受支持接口，因此不能仅删除插件 MCP 客户端，就保留命令、身份校验和自动发图功能。

宿主机服务仍需保留：它负责调用 Codex、隔离绘图配置、管理任务与图片。MaiBot 原生 MCP 是连接客户端，不会代替这些执行逻辑，也不会让容器直接启动宿主机进程。

## 验证结果

| 项目 | 结果 |
| --- | --- |
| 原生 Streamable HTTP + Bearer 连接现有服务 | 通过实际本机测试 |
| 发现 submit / status / result / health 四个工具 | 通过实际本机测试 |
| 签名请求提交、查询及取回模拟绘图 | 通过实际本机测试 |
| 原生结果保留 PNG ImageContent | 通过，保留 110 字节模拟 PNG |
| 插件通过 SDK 选择并调用主程序 MCP 连接 | 当前无受支持接口 |
| 原生图片进入模型上下文、监控及 reply 附件路径 | 有源码路径；聊天平台端到端未验收 |
| Docker 到宿主机网络、真实 Codex 绘图 | 不在本次验证范围 |

本机测试使用已安装 MaiBot 1.3.15。SDK 2.16.0 / 2.17.0 均无 MCP 调用代理。子代理也核对了当时的上游 SDK 2.17.0 与 MaiBot 1.3.16 源码，仍未发现相应 Host capability。不要把此结论泛化到未经检查的未来版本。

## 被检版本的能力缺口

- SDK 没有 ctx.mcp；ctx.tool 只能查询定义，ctx.api 是插件间公开 API，不会自动转发到原生 MCP。
- 插件在独立 Runner 进程内运行，直接导入内部 MCP 单例不能复用主程序的进程级连接。
- 授权 config.get 后可以读取原生 MCP 服务器配置，减少 URL 和 token 的重复配置，但仍需插件自己的客户端。这需要后续插件实现，目前版本尚未采用。
- 当前绘图工具需要签名 envelope。原生工具链转发模型参数，不会自动把真实用户、聊天流与源消息编号变成服务可验证的身份。不能让模型拿密钥签名或自行选择身份来替代这部分逻辑。
- 原生图片内容能保留，也存在 reply.attach_pic 引用工具图片的路径；这尚不等于当前插件的任务轮询、定向发送、回执和未知结果锁已被替代。

## 在不改本体、不改镜像的条件下

0.4.0 已将 **宿主机 MCP + 宿主机 Codex + MaiBot 薄插件** 定为统一架构。容器内不需要 Codex，插件不启动内置服务，宿主机进程由管理员单独运行；插件配置仅保留连接字段及启用、配置版本信息。

本版边界是将服务端、Uvicorn 启停和本地 Codex 管理交给宿主机独立服务，具体代码与工具适配需单独核查。读取原生 MCP 配置仍是后续方向。若要求连插件客户端也去掉，需要等待或采用上游正式开放的 SDK/Host MCP 调用能力，再验证身份传递和图片交付。

参考证据：

- [MaiBot MCP 配置文档](https://docs.maibot.chat/manual/configuration/mcp-config)
- [SDK 2.17.0 能力入口（固定提交）](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/07821c0bcd14e9799cd219112a913040255c88f7/maibot_sdk/context.py#L175)
- [上游 Host 能力注册（固定提交）](https://github.com/Mai-with-u/MaiBot/blob/fe88ec6b6053e40eaf430cb5ba7ed7e805add2cb/src/plugin_runtime/capabilities/registry.py#L98)
