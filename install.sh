#!/bin/sh
# Arguments are forwarded to deploy.py. Codex login is always manual.
set -eu
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${MAIBOT_IMAGE_INSTALL_DIR:-"$HOME/MaiBot-Codex-Image"}
# Resolve --install-directory early so bootstrap and managed files share one location.
previous=''
for argument in "$@"; do
    if [ "$previous" = '--install-directory' ]; then install_dir=$argument; fi
    previous=$argument
done
mkdir -p "$install_dir"
install_dir=$(CDPATH= cd -- "$install_dir" && pwd)
uv_binary=${MAIBOT_IMAGE_UV:-''}
if [ -z "$uv_binary" ]; then uv_binary=$(command -v uv || true); fi
if [ -z "$uv_binary" ]; then
    mkdir -p "$install_dir/bootstrap"
    uv_binary="$install_dir/bootstrap/uv"
    if [ ! -x "$uv_binary" ]; then
        curl --fail --location --proto '=https' 'https://astral.sh/uv/0.13.6/install.sh' -o "$install_dir/bootstrap/uv-installer.sh"
        UV_UNMANAGED_INSTALL="$install_dir/bootstrap" sh "$install_dir/bootstrap/uv-installer.sh"
    fi
fi
export UV_CACHE_DIR="$install_dir/cache"
export UV_PYTHON_INSTALL_DIR="$install_dir/python"
exec "$uv_binary" run --no-project --python 3.13 "$bundle_dir/deploy.py" \
    --install-directory "$install_dir" --uv "$uv_binary" "$@"
