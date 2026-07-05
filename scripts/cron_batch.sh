#!/usr/bin/env bash
# 四批备刊模型（达拉斯时间，见 README 部署段）：
#   批1 00:01  翻牌今日刊 + 自愈 + 备明日刊(学术+艺术)   ← 慢内容，对隔天见刊最不敏感
#   批2 05:00  备明日刊(开源+数据)
#   批3 10:00  备明日刊(量化)
#   批4 15:00  备明日刊(全板块收尾：科技+商业 + 补齐之前批次失败的板块)  ← 美股收盘
# 每批 ≈15-25 次 LLM 调用，正好落在 Codex 订阅的一个 5 小时额度窗口内。
#
# 用法: cron_batch.sh <1|2|3|4>
# 可选环境变量:
#   HEALTHCHECK_URL  healthchecks.io 死人开关前缀，成功后拼上批号 ping 对应 check。
#                    形如 https://hc-ping.com/<ping-key>/rebas-batch-（末尾连字符），
#                    对应 4 个 check 的 slug = rebas-batch-1 .. rebas-batch-4。
#                    注意不能用单 check 的 UUID 直拼——/1 会被判成失败退出码。
#   DEPLOY_CMD       翻牌后执行的部署命令（如推 GitHub/Cloudflare Pages）
#   REBAS_NPM        npm 路径（cron 精简 PATH 找不到 nvm 时用）

set -euo pipefail
export TZ=America/Chicago PYTHONUTF8=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
REBAS=".venv/bin/rebas"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
BATCH="${1:?用法: cron_batch.sh <1|2|3|4>}"
TOMORROW="$(date -d tomorrow +%F)"

# 批次间互斥（publish 自带进程锁，这里连 collect 一起罩住）
exec 9>"$ROOT/data/cron.lock"
flock -w 300 9 || { echo "锁等待超时，跳过本批"; exit 1; }

echo "=== batch $BATCH $(date '+%F %T %Z') tomorrow=$TOMORROW ==="

# collect 单源出错属常态（付费墙/偶发 5xx），不让它中断批次
$REBAS collect || echo "[warn] collect 有源出错（单源级，已隔离）"

case "$BATCH" in
  1)
    $REBAS publish || echo "[warn] 今日刊自愈失败，翻牌将展示最近完整期次"
    $REBAS render                       # 翻牌：昨天备好的刊此刻上线（零 token）
    if [ -n "${DEPLOY_CMD:-}" ]; then $DEPLOY_CMD; fi
    $REBAS publish --date "$TOMORROW" --boards academic,art
    ;;
  2) $REBAS publish --date "$TOMORROW" --boards repos,data ;;
  3) $REBAS publish --date "$TOMORROW" --boards quant ;;
  4) $REBAS publish --date "$TOMORROW" --refill ;;
     # 全板块收尾：科技+商业+扫尾，推进状态；--refill=补充轮，
     # 前三批备的板块若选题<refill_min_topics，用白天新采集的候选补选（选题够则不动）
  *) echo "未知批次 $BATCH"; exit 1 ;;
esac

if [ -n "${HEALTHCHECK_URL:-}" ]; then
  curl -fsS -m 10 "${HEALTHCHECK_URL}${BATCH}" >/dev/null || true
fi
echo "=== batch $BATCH done $(date '+%F %T %Z') ==="
