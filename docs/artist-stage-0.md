# 小画师阶段 0 环境核查

核查日期：2026-10-31，Asia/Hong_Kong。插件开发基线为 `9e3112ac0c86bddd4c576caac674a3d1ac810eed`，本轮工作树增加阶段 0/1，未部署。核查只读源码与安装元数据；没有运行安装实例、连接 MCP、读取登录凭据或真实任务库。

## 已核实的层级

| 对象 | 版本和证据 | 能证明什么 |
| --- | --- | --- |
| 插件源码 | manifest、pyproject 声明 0.7.0；Git HEAD 9e3112a | 当前维护基线，不代表部署 |
| 插件开发环境 | 根 .venv：CPython 3.13.18、SDK 2.16.0、MCP SDK 1.30.0、httpx 0.28.1、pytest 9.1.1、pytest-asyncio 1.4.1 | 本轮离线测试环境；SDK 锁定提交 835e0e9d1f70ea00885fb49d059f7f2864976d05 |
| 本地 MaiBot 源码 | maibot-source：062d45cf8bd4272ac57d7ec03fcf405e5a458211，pyproject 1.3.15；稀疏检出 | 源码版本，不是已运行进程的版本 |
| 本地已安装实例 | local-maibot/tools/maibot/Lib/site-packages 元数据：MaiBot 1.3.15、SDK 2.17.0 | 包安装版本；未证明正在使用 |
| 实例插件 | local-maibot/instance/plugins/codex_image/_manifest.json：0.2.0 | 不是当前插件 0.7.0；不将其当作新方案验收环境 |
| 用户提供的 MCP 候选目录 | codex-image-mcp 的 pyproject 与安装元数据：0.1.0；editable 映射到该目录 src | 静态安装与启动入口已定位，未确认 live 归属；不凭版本号判定不兼容 |
| 残留服务环境 | 插件目录 service/.venv 安装元数据：MCP 0.5.0 | 仅安装残留，不证明服务运行或可重启，不修改它 |
| 可见 Codex CLI | codex --version：0.166.0-alpha.5.1 | 当前命令解析到的工具，不证明 MCP 正在使用、已登录或支持原图编辑 |
| 可见 Docker CLI | docker --version：29.8.0 | CLI 存在，不证明 daemon 或容器可访问 |
| uv | 当前 PowerShell PATH 无 uv；用户线索核查找到 codex-image-mcp/.venv/Scripts/uv.exe，绝对路径执行版本为 0.13.6 | 本轮测试仍使用现有插件 .venv，不安装或同步依赖 |

上轮接口分析使用 MaiBot 上游固定提交 `c6429547af711555e3d4d1b8d04b413572cb616c` 和 SDK 2.20.0 源码；它们不是本轮已安装或已运行版本。阶段 1 仅使用标准库，不改变 SDK 范围，未来 Host 接入仍需针对实际版本验证。

用户补充 UV 安装与 `codex-image-mcp` 目录线索后，对照了 [官方 UV 部署说明](https://docs.maibot.chat/manual/deployment/uv-tool)。UV 的工具安装环境与存放配置、数据、plugins 的实例目录可以分离，启动还可指定 `--directory`。因此需要同时识别工具包位置和实例入口，不能仅凭当前工作目录判定加载版本。

## UV 安装与静态启动链

读取 local-maibot/launch_local.py 的参数 AST 和 UV 安装收据，未执行启动器：

- MaiBot：local-maibot/bin/maibot.exe → local-maibot/tools/maibot/Scripts/python.exe → src.cli.tool:main；启动参数为 run --directory local-maibot/instance。安装收据位于 local-maibot/tools/maibot/uv-receipt.toml。
- MCP：codex-image-mcp/.venv/Scripts/python.exe → -m codex_image_mcp.server；editable 包映射到 codex-image-mcp/src，安装器记录为 uv。

候选 MCP 源码已具有 codex_image_submit/status/result/health、envelope、三元 scope 和 request_id/prompt 入口，与声明 0.1.0 不能简单等同为不支持当前接口。不过其 auth.py 仍检查服务端 allowed_users，且源码与旧 MCP 0.5.0 基线不完全相同，不能将 0.5.0 的测试结论沿用到它。未读取实际名单、调用候选服务或验证生成。

用户已补充条件授权：若实际 MCP 与目标插件不兼容，可重新部署匹配版本。本轮阶段 1 没有改变运行协议；缺少未来 edit/lookup 不算当前不兼容。先确认 live 归属与具体不兼容项，再触发服务维护；本轮未达到该条件，没有重部署。

## 实际运行环境的核实缺口

阶段 0 的本机源码与安装基线已确认，但没有取得正在运行的 MaiBot/MCP 版本证据。首次受限环境的 CIM 查询返回“无法从客户端中访问 CIM 资源”，备用枚举仅有三个可见进程；随后在正常环境完成只读复核，CIM 成功读取 534 个进程，其中 Python/MaiBot/uv 前缀进程 4 个，按相关模块和目录关联后未发现本项目运行候选。匹配仅在内存处理，未输出完整命令行。

在受限和正常环境中，对本机 docker_engine 与 dockerDesktopLinuxEngine 两个命名管道的只读容器查询均返回 The system cannot find the file specified，未取得容器 ID，也未进行容器 inspect。当前本机未识别运行候选，不证明其他主机、上下文或部署位置没有运行服务。

需要实际运行 MaiBot/MCP 的主机及服务名、容器名或维护目录，将运行进程与加载版本、源码入口对应起来。无需提供密钥。未识别入口前不联猜测的远端、不修改挂载、不启动历史环境，也不通过真实绘图探测版本。

阶段 1 的独立 SQLite 和策略测试可继续；阶段 2 的 Host 接入、阶段 3/4 的服务维护及任何部署仍需先补齐对应运行证据。MCP 后续维护不能在插件的历史测试快照中进行。

## 复查方法

- Git：对插件和主源码分别执行 rev-parse HEAD、status --short；对主源码读取 sparse-checkout list。
- Python：用已存在的根 .venv/Scripts/python.exe，读取 sys.version 和指定 site-packages 的 importlib.metadata，只筛选包名和版本，不导入或启动产品。
- 插件：仅解析源码和实例的 _manifest.json 版本与兼容字段。
- 运行：读取进程名、可执行路径以及选择性的 Docker ps 字段；不输出环境变量、完整命令行、服务配置或整个容器 inspect。

本轮没有修改 MaiBot、独立 MCP、实际配置或运行环境。源码验证、安装元数据、运行进程、端到端能力四种证据不可互相替代。
