#!/usr/bin/env bash
# 本机 → VPS 代码同步。用法: scripts/vps_sync.sh <user@host> [远端路径，默认 /opt/rebas_daily]
#
# 【交接语义，2026-07-04 上线后】VPS 是数据与凭证的唯一正本：
#   - data/（数据库）与 .codex/（auth 会在 VPS 上自动刷新）、.secrets/ 默认**不再同步**，
#     防止用本机旧副本覆盖线上状态；
#   - 首次部署（bootstrap 前）需要搬家时，用 --with-secrets 显式带上它们。

set -euo pipefail
WITH_SECRETS=0
if [ "${1:-}" = "--with-secrets" ]; then WITH_SECRETS=1; shift; fi
HOST="${1:?用法: vps_sync.sh [--with-secrets] <user@host> [remote_path]}"
DEST="${2:-/opt/rebas_daily}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

EXTRA=(--exclude 'data/' --exclude '.codex/' --exclude '.secrets/')
if [ "$WITH_SECRETS" = 1 ]; then
  EXTRA=(--exclude 'data/logs/' --exclude 'data/publish.lock' --exclude 'data/*.bak-*'
         --exclude '.codex/log/' --exclude '.codex/sessions/' --exclude '.codex/cache/'
         --exclude '.codex/tmp/' --exclude '.codex/.tmp/' --exclude '.codex/shell_snapshots/')
fi

ssh "$HOST" "mkdir -p '$DEST'"
# 注意不要用 --delete-excluded：bootstrap 后 VPS 上有本机排除的 .venv/node_modules，会被误删
rsync -az --info=progress2 \
  --exclude '.git/' \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.pytest_cache/' --exclude 'src/rebas.egg-info/' \
  --exclude 'web/node_modules/' --exclude 'web/.astro/' --exclude 'web/data/' \
  --exclude 'site/' \
  "${EXTRA[@]}" \
  "$SRC/" "$HOST:$DEST/"

if [ "$WITH_SECRETS" = 1 ]; then
  ssh "$HOST" "chmod 700 '$DEST/.codex' '$DEST/.secrets'"
fi

# admin 是常驻服务，代码/配置同步后必须重启，否则新旧版本错位
# （2026-07-05 实际踩坑：旧进程的 Source 类读不了带新字段的 sources.toml，后台 500）。
# 管线进程每次冷启动，无需处理；服务不存在（如首次部署前）则跳过。
ssh "$HOST" "systemctl try-restart rebas-admin 2>/dev/null || true"
echo "✔ 同步完成 → $HOST:$DEST"
