#!/bin/sh
# Run on the host, from a checkout in MaiBot's existing plugins mount.
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v uv >/dev/null 2>&1; then
    echo '未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 1
fi
automatic_systemd=yes
for argument in "$@"; do
    case "$argument" in
        --foreground|--systemd) automatic_systemd=no ;;
        --help|-h)
            echo '用法：bash setup.sh [--container 容器名] [--allowed-user qq:用户ID]'
            echo '可选：--network 网络 --data-dir 原服务配置目录 --foreground --systemd'
            echo 'Linux root 默认配置 systemd；源码放 plugins/codex_image，脚本在宿主机运行。'
            echo '重复使用保留原登录、密钥和任务；仅更新插件时无需运行本脚本或重启 MCP。'
            exit 0 ;;
    esac
done
if [ "$automatic_systemd" = yes ] && [ "$(uname -s)" = Linux ] && [ "$(id -u)" = 0 ]; then
    set -- "$@" --systemd
fi
# The setup tool has its own uv cache environment. Running it again must not
# synchronize dependencies underneath an already-running MCP process.
exec uv run --no-project --python 3.13 --with 'tomlkit>=0.13,<1' \
    python "$source_dir/scripts/setup_host.py" --prepare-environment "$@"
