# 插件目录与绘图服务边界

当前目录调整用于 0.11.1；配置结构仍为 0.11.0，账本仍为 schema 9。源码组织调整不迁移用户数据，不改变授权、额度、任务编号或未知发送语义。

## 目录职责

```text
codex_image/
├── plugin.py                 # 工厂入口、配置模型、组件和生命周期
├── _manifest.json            # 版本、依赖、Host 能力授权
├── webui.json                # 工作区页面/API 声明
├── assets/studio.html        # 自包含工作区页面
├── domain/                   # 状态、权限、预算、档案与素材规则
│   ├── policy.py
│   ├── store.py
│   ├── delivery.py
│   ├── profiles.py
│   └── assets.py
├── creation/                 # 规划、评审、绘图协调及恢复
│   ├── artist.py
│   ├── thinking.py
│   ├── rendering.py
│   ├── editing.py
│   ├── recovery.py
│   └── references.py
├── host/                     # SDK、Hook、会话和发送适配
│   ├── host_runtime.py
│   ├── host_adapters.py
│   ├── host_media.py
│   ├── studio_chat.py
│   └── group_thought_runtime.py
├── inspiration/              # 群念头存储及历史实验采集路径
│   ├── group_thoughts.py
│   ├── group_notebook.py
│   ├── group_ingress.py
│   └── group_collection.py
├── mcp_client/bridge.py       # 签名、能力协商与 MCP 协议
├── workspace/studio.py       # 管理工作区的只读查询模型
├── tests/                    # 隔离回归，按功能命名
├── scripts/build_bundle.py   # 显式发行白名单
├── docs/                     # 使用、架构与历史验证
├── config.example.toml       # 无密钥的配置示例
├── requirements.txt
├── pyproject.toml
└── uv.lock
```

每个 Python 子包包含 `__init__.py`，包初始化只含说明，不创建任务或数据库。模块文件名保留，迁移规则为对应子目录加原文件名。根 `studio.html` 移至 `assets/studio.html`，同步 `webui.json.entry`、测试、打包白名单和源码冻结检查。

这些目录按职责归类，不宣称已消除所有跨层依赖。例如创作协调仍识别 Bridge 错误码，素材规则仍复用 Host 媒体校验。本次不为目录外观重写接口、重复实现图片验证或引入动态兼容导入。

## 与 MaiBot 插件约定的关系

[开发指南](https://docs.maibot.chat/plugin/)要求根入口和清单，支持独立静态资源目录；[生命周期](https://docs.maibot.chat/plugin/lifecycle)由 `create_plugin()` 和三个生命周期方法连接 Host。[WebUI 页面](https://docs.maibot.chat/plugin/webui-pages)允许插件内部相对 HTML 路径，页面继续内联脚本和样式。

内部使用包内相对导入，适配 Host 生成的模块命名空间，不导入 MaiBot 的 `src.*`。仅测试可显式接入公开 Host 源码。跨目录导入不改成机器路径、固定仓库包名或 `sys.path` 注入。

数据库仍由 Host 的 `ctx.paths.data_dir` 定位；源码路径与数据路径独立。配置由 Runner 管理，根 `config.toml` 不进入 Git 或发行包。目录变化不要求重建 SQLite、重置额度或重新发送旧图。

## 升级与回退

- 按完整发行文件集升级，同时包含六个子包、入口和 `assets/studio.html`；只覆盖 `plugin.py` 会缺模块。
- 使用现有维护流程卸载插件后切换源码，再由 Host 加载，避免热重载观察到半套文件。保留原配置和 Host 数据目录；独立绘图服务不用因目录整理重启。
- 对已经授权的生产实例，维护暂停使用 Host 的插件启用开关卸载插件，保留原 `autonomy.enabled`；不能把关闭自主创作当作无副作用的暂停。当前关闭自主创作会撤销持久委托，重新启用会改变授权及参考素材策略版本，冻结的旧计划可能被 `REFERENCE_AUTH_CHANGED` 拦截。先备份，再确认卸载及账本未变，隔离测试结束后恢复原配置；不要改写旧版本号来绕过校验。
- 旧根模块已从新发行清单移除，叠加解压可能留下旧文件。新导入路径不会引用它们；后续维护以完整新包为准，不要为清旧源码而递归删除含配置或数据的安装目录。
- 回退时恢复完整旧源码及旧 `webui.json`，不只回退入口。此次没有新增数据迁移，schema 9 和未知记录保留。
- 0.11.0 冻结文件和部署包摘要仍按原路径解释，不能视为重组后摘要。当前验证另记于[审查记录](plugin-audit-20261105.md)。

## MCP 是否必须存在

MaiBot 插件本身不强制使用 MCP。小画师通过 Host 的模型、Hook 和发送能力自主决策；MCP 是插件与外部绘图服务之间的协议。可以替换协议，但删除协议不等于删除持久绘图服务。

| 方案 | 可行性及代价 |
| --- | --- |
| 保留独立服务及 MCP | 当前有恢复、幂等、原图编辑与发送记录验证；本次保留 |
| 独立服务改为专用 HTTP API | 技术上可行，可减少 MCP 会话、工具和内容块封装；必须同时升级服务与客户端，保留鉴权、任务查询、原图字节、错误码、幂等及能力协商 |
| 插件直接运行 Codex CLI | 同机可另行设计；当前容器与宿主机分离部署须处理 CLI、登录态、产物路径与进程所有权，插件重载还不能丢失在途任务；改动远大于移除依赖 |
| 用 MaiBot 通用 MCP 工具替代整个插件 | 不能直接替代私有账本、预算、评审、群隔离和分享权限，外部绘图工具也不能绕过这些约束 |

建议保留独立服务。如果目标是完全去除 MCP 协议，可单独实施 HTTP 迁移：服务先增加共用原任务库的接口，插件再引入等价客户端，最后关闭旧协议。切换沿用原 scope、request_id、job_id 和数据目录；未知任务先只读恢复，不能提交新任务来判断旧状态。新旧服务配对、超时恢复、编辑输入和重复请求均通过后，才能移除 `mcp` 依赖。

过渡期两个协议入口应在同一服务进程中共用一个任务管理器和目录锁，不能为HTTP再启动一个读写相同state的worker。九项操作为 `submit/edit/status/result/health/lookup/asset_register/asset_lookup/render`，不能只实现前三项就宣称等价。现有 `scope/op/args/iat` 签名、归属校验以及代理/重定向限制也须保留；协议切换不自动放宽授权。

本次只把现有客户端集中到 `mcp_client/`，没有实现 HTTP 替代接口、删除 MCP、部署服务或调用真实 Codex。

2026-11-06 的部署决定是继续保留 MCP；本轮只升级插件。协议替代内容保留为技术评估，不是当前实施计划。

迁移还有一个现有约束：`host/host_runtime.py` 用连接 URL 的哈希固定账本服务绑定，`domain/store.py` 拒绝将原空间绑定到不同服务。直接把 `/mcp` 改为新 HTTP 地址可能得到 `OWNER_CONFLICT`；必须先设计显式、保留旧归属的服务身份迁移，或在不变地址下协商传输，不能清空旧绑定绕过检查。本次分目录不改变 URL 配置。
