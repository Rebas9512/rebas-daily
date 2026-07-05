#!/usr/bin/env bash
# 站点部署：site/ 直传 Cloudflare Pages（免费层；构建已在本机完成，不占 CF 构建配额）。
# cron_batch.sh 批 1 翻牌后经 DEPLOY_CMD 调用；也可手动执行。
#
# 凭证从 .secrets/.env 读取（headless，不走 wrangler login 浏览器流）：
#   CLOUDFLARE_API_TOKEN=<API Token，权限 Account → Cloudflare Pages → Edit>
#   CLOUDFLARE_ACCOUNT_ID=<账户 ID，dash.cloudflare.com 任意域名页右侧栏可查>
# 可选：PAGES_PROJECT=<Pages 项目名>（默认 rebasdaily）

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

set -a
[ -f "$ROOT/.secrets/.env" ] && . "$ROOT/.secrets/.env"
set +a
: "${CLOUDFLARE_API_TOKEN:?缺 CLOUDFLARE_API_TOKEN（放 .secrets/.env）}"
: "${CLOUDFLARE_ACCOUNT_ID:?缺 CLOUDFLARE_ACCOUNT_ID（放 .secrets/.env）}"

[ -f "$ROOT/site/index.html" ] || { echo "site/ 为空，先 rebas render"; exit 1; }

# --commit-dirty：部署与 git 状态无关（构建产物目录本就不进版本库）
exec wrangler pages deploy "$ROOT/site" \
  --project-name "${PAGES_PROJECT:-rebasdaily}" \
  --branch main --commit-dirty=true
