# 验证记录

## 小画师阶段 0 和阶段 1

2026-10-31，基于插件 9e3112a 的未发布工作树：新增状态与策略底座，尚未接入运行路径。主线完整回归 95 passed，新增阶段 1 集成 44 passed，Ruff 所选规则通过；独立审查及环境核实范围见 [阶段 1 记录](implementation-stage-1.md) 和 [阶段 0 记录](artist-stage-0.md)。未修改 MaiBot 或 MCP，未部署、迁移真实数据或调用真实绘图。

## 0.7.0 精简的历史验证

日期：2026-10-30（Asia/Hong_Kong）。对象为基于 `e50a7b6f0e090d6a5053ac00d4f0b14e92aee272` 的本地 0.7.0 工作树；未提交、发布或部署。

## 本轮结果

| 层次 | 结果与范围 |
| --- | --- |
| 运行逻辑与元数据 | 三个运行模块的 AST 与基线一致，仅调整配置默认版本及模块说明；manifest、项目版本、配置示例均为 0.7.0，运行依赖保持 httpx / mcp |
| 锁定开发环境 | Windows、CPython 3.13.18、uv 0.13.6；根 `uv sync --locked` 成功，SDK 2.16.0 固定 commit `835e0e9d1f70ea00885fb49d059f7f2864976d05`，MCP SDK 1.30.0 |
| 测试作者自测 | 新 `tests/`：51 passed |
| 主智能体集成复跑 | 根锁定环境执行新 `tests/`：51 passed（8.20s） |
| 旧 MCP 兼容回归 | 原 MCP 0.5.0 服务源码配合模拟 Codex：18 项插件回归通过；另 1 项独立进程升级测试通过 |
| 静态检查 | Ruff 所选 `E4,E7,E9,F` 通过；未将默认全规则检查标为通过。`git diff --check`、版本/依赖一致性及文档相对文件链接检查通过 |
| 独立只读审查 | PASS，未发现本次精简引入的阻塞问题；未参与实现的审查者核对固定快照、验证文档及插件包，读取集成 XML，未复跑测试 |
| 插件打包 | 单一插件 zip、14 个文件；白名单及逐项字节一致，相对文档链接有效，SHA256SUMS 校验通过；不含服务、部署工具、测试或私密数据 |
| 目标部署 / Docker 连通 | 未执行 |
| 真实 Codex 绘图 / MaiBot 实际聊天收图 | 未执行；本轮没有调用真实 Codex 或消耗绘图额度 |
| 用户验收 | 未完成，需用户明确确认 |

独立审查使用 24 文件快照，其逐文件 SHA-256 清单的 SHA-256 为 `08f4e4c620c9d6c8b373d4b09b8a467310ac2fea0b77d785911612a92bcfa821`。之后只补充文档中的本轮验证结果与检查命令；代码、构建器、依赖及测试按该快照核对。

## 测试具体覆盖

根测试使用真实 FastMCP / Streamable HTTP 传输，以及内存任务、静态 PNG 和模拟 Host 发送接口。覆盖可信身份、空名单与未授权拒绝、签名字段及操作/时效绑定、跨用户/平台/聊天拒绝、请求去重、自动取图、手动重发原图、重载保留发送记录、取消与未知发送锁定、Bridge 错误响应，以及打包排除私密文件。

该协议夹具不是生产服务，不证明独立服务实现正确。为检查旧服务兼容，额外从基线提交导出完整跟踪源码到忽略的本地验证目录，并用本版三个运行模块替换其插件文件，再运行原服务的插件测试。实际 MCP 服务源码未改，只有 Codex 可执行端使用原 `fake_codex.py`；所有任务、登录目录和发送记录均为临时合成数据。

18 项回归来自原 `test_mcp_plugin.py`、`test_plugin_authorization.py` 和 `test_external_plugin.py`（排除旧双包构建用例）。另行运行原 `test_plugin_only_upgrade.py`，验证客户端更新前后服务 PID / 启动标识 / worker 不变，旧任务、图片摘要、归属和幂等仍有效，没有第二次绘图 RPC。Windows 的 venv `python.exe` 是启动器，测试副本改为在 ready 文件记录实际服务 PID，再与响应 PID 比较；未改服务逻辑。

## 复现与本地产物

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check --select E4,E7,E9,F plugin.py bridge.py delivery.py scripts/build_bundle.py tests
uv run --locked python scripts/build_bundle.py
```

本机沙箱默认临时目录不能供 pytest 正常创建编号目录，因此主智能体复跑增加 `--basetemp=dist/validation/pytest-integration --junitxml=dist/validation/pytest-integration.xml`。每次复跑应使用新的临时目录；pytest 会清理指定 basetemp，不得指向现有数据。

作者与集成 XML、旧服务基线副本、审查快照均仅保存在忽略的 `dist/validation/`，不是源码或发行内容。最终插件 zip 与 `SHA256SUMS` 位于 `dist/`；不包含实际配置、密钥、登录、任务库、发送库或开发环境。原有未跟踪的本地规则和忽略的服务环境/状态未清理。

## 升级与验证边界

若旧 MCP 仍引用原仓库 `service/`，不可原地更新删除运行入口；保留原完整目录给服务，新插件单独部署，并保留 Host 插件身份、原数据目录和发送记录。详见 [升级指南](quickstart.md)。本轮未检查或迁移任何实际运行的 MCP。

健康检查、模拟协议、任务完成、Host 返回发送成功与聊天端实际收图分别记录。声明的 Host / SDK 版本范围未逐一实测；本轮只绑定上述锁定版本。旧验证记录见 [基线提交](https://github.com/PargosAks/MaiBot-Codex-Image/blob/e50a7b6f0e090d6a5053ac00d4f0b14e92aee272/docs/validation.md)，不沿用为本版真实部署或用户验收证据。
