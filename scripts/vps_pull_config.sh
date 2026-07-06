#!/usr/bin/env bash
# 把 VPS 上经管理后台改过的配置拉回本地（入 git，防下次 vps_sync 覆盖线上改动）。
# 用法: scripts/vps_pull_config.sh [<user@host>] [远端路径，默认 /opt/rebas_daily]
# 主机缺省时读 .secrets/.env 的 VPS_HOST（不入 git）。
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_HOST="$(grep -oP '^VPS_HOST=\K.+' "$SRC/.secrets/.env" 2>/dev/null || true)"
HOST="${1:-$DEFAULT_HOST}"
[ -n "$HOST" ] || { echo "用法: vps_pull_config.sh <user@host> [remote_path]（或在 .secrets/.env 配 VPS_HOST）" >&2; exit 1; }
DEST="${2:-/opt/rebas_daily}"

rsync -av "$HOST:$DEST/config/profiles/" "$SRC/config/profiles/"
rsync -av "$HOST:$DEST/config/config.toml" "$SRC/config/config.toml"
echo "✔ 已拉回 config/profiles/ 与 config.toml —— 记得 git diff 检查后提交"
