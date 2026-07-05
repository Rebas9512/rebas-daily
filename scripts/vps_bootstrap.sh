#!/usr/bin/env bash
# 全新 Ubuntu 24.04 VPS 一键就绪。在 vps_sync.sh 同步完成后，于 VPS 上以 root 执行：
#   bash /opt/rebas_daily/scripts/vps_bootstrap.sh
#
# 做的事：时区 → 系统依赖 → Node 22 → codex/wrangler 全局安装 → venv →
#         前端依赖 → 凭证冒烟 → 装 crontab（四批备刊 + 部署钩子）。
# 幂等：重复执行安全。

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== 时区（刊物日历 = 达拉斯）=="
timedatectl set-timezone America/Chicago

echo "== 系统依赖 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv curl ca-certificates rsync  # 泛化包名，24.04/26.04 均可

echo "== Node 22（Astro 5 / wrangler 需要 ≥20，发行版自带的偏旧）=="
if ! command -v node >/dev/null 2>&1 \
   || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
fi
npm install -g --silent @openai/codex wrangler

echo "== Python 环境 =="
cd "$ROOT"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -e .

echo "== 前端依赖 =="
(cd web && npm ci --no-fund --no-audit --silent)

chmod 700 .codex .secrets
mkdir -p data/logs

echo "== Codex 凭证冒烟（拷来的 .codex/ 是否在本机可用）=="
bash scripts/verify_codex.sh status

echo "== 安装 crontab（四批备刊，达拉斯时刻）=="
crontab - <<EOF
PATH=/usr/local/bin:/usr/bin:/bin
DEPLOY_CMD=$ROOT/scripts/deploy_site.sh
# HEALTHCHECK_URL=https://hc-ping.com/<uuid>   # 建好 check 后 crontab -e 取消注释

1  0  * * *  $ROOT/scripts/cron_batch.sh 1 >> $ROOT/data/logs/batch.log 2>&1
0  5  * * *  $ROOT/scripts/cron_batch.sh 2 >> $ROOT/data/logs/batch.log 2>&1
0  10 * * *  $ROOT/scripts/cron_batch.sh 3 >> $ROOT/data/logs/batch.log 2>&1
0  15 * * *  $ROOT/scripts/cron_batch.sh 4 >> $ROOT/data/logs/batch.log 2>&1
0  1  1 * *  tail -c 1M $ROOT/data/logs/batch.log > /tmp/b.log && mv /tmp/b.log $ROOT/data/logs/batch.log
EOF
crontab -l | head -4

cat <<'DONE'

✔ VPS 就绪。剩两步手工事项：
  1) Cloudflare 凭证 → 追加进 .secrets/.env：
       CLOUDFLARE_API_TOKEN=<dash → My Profile → API Tokens → Create（模板 Cloudflare Pages:Edit）>
       CLOUDFLARE_ACCOUNT_ID=<dash 任意域名页右侧栏>
  2) 全链路验证（跑一次收尾批，含出刊+构建+部署）：
       bash scripts/cron_batch.sh 4
  可选：healthchecks.io 建 check 后 crontab -e 填 HEALTHCHECK_URL（批次失败邮件告警）。
DONE
