# 开发与构建

根目录是标准 MaiBot 插件入口；embedded.py 将 service/src 的 MCP 服务作为包内模块加载，由插件生命周期管理。service 同时保留独立运行方式。client.py 负责发现原生客户端，login.py 仅由用户在终端手动运行。deploy.py、install.ps1 和 install.sh 是可选外部服务部署工具。

```sh
uv sync --project service --locked
uv run --project service pytest service/tests -q
```

测试使用真实 MCP 网络握手、独立模拟 Codex 子进程和实际 MaiBot SDK 配置，插件按 Runner 的隔离包方式导入。模拟测试不会消耗订阅额度。

安装测试检查配置备份、重复部署保留凭证和任务、路径越界拒绝，以及只修改指定插件和绘图命令权限。

```sh
python scripts/build_bundle.py
```

构建器用文件白名单生成完整部署包、含内置 MCP 源码的插件包和 SHA256SUMS。插件包可用于 MaiBot ZIP 安装；插件中心的 GitHub Release 源码包也包含同样的内置源码。包内不含运行数据、密钥、登录、缓存、虚拟环境、.git 或本地会话。

不得为插件兼容去修改 MaiBot 本体代码。安装依赖在 service/uv.lock 中锁定；开发用 SDK 固定上游公开提交。
