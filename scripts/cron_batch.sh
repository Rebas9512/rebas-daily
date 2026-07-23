#!/usr/bin/env bash
# 备刊批次模型（达拉斯时间，见 README 部署段）：
#   批1 00:01  翻牌今日刊 + 自愈 + 备明日刊(学术+艺术)   ← 慢内容，对隔天见刊最不敏感
#   批2 05:00  备明日刊(开源+数据；累积带上批1板块=顺延其未完成的)
#   批3 10:00  备明日刊(量化；累积带上批1-2板块)
#   批4 15:00  备明日刊(全板块收尾：科技+商业 + 补齐之前批次失败的板块)  ← 美股收盘
#   批5 20:00  兜底收尾：批4没做完(额度耗尽/中途失败)时补完剩余
# 每批 ≈15-25 次 LLM 调用，正好落在 Codex 订阅的一个 5 小时额度窗口内。
# 顺延/兜底不做显式检测：靠 issue 状态检查点 + 板块级幂等守卫——做过的
# 零 token 跳过、没做完的自动重试，一切正常时批5是纯空转。
#
# 用法: cron_batch.sh <1|2|3|4|5>
# 可选环境变量:
#   HEALTHCHECK_URL  healthchecks.io 死人开关前缀，成功后拼上批号 ping 对应 check。
#                    形如 https://hc-ping.com/<ping-key>/rebas-batch-（末尾连字符），
#                    对应 check 的 slug = rebas-batch-1 .. rebas-batch-5。
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
BATCH="${1:?用法: cron_batch.sh <1|2|3|4|5>}"
TOMORROW="$(date -d tomorrow +%F)"

# 批次间互斥（publish 自带进程锁，这里连 collect 一起罩住）
exec 9>"$ROOT/data/cron.lock"
flock -w 300 9 || { echo "锁等待超时，跳过本批"; exit 1; }

echo "=== batch $BATCH $(date '+%F %T %Z') tomorrow=$TOMORROW ==="

# collect 单源出错属常态（付费墙/偶发 5xx），不让它中断批次
$REBAS collect || echo "[warn] collect 有源出错（单源级，已隔离）"

case "$BATCH" in
  1)
    # zone 流量对照层拉取（T+1 数据，一天一次够）；没配 token 时命令自身静默跳过
    $REBAS traffic-pull || echo "[warn] zone 流量拉取失败（监控层，不影响出刊）"
    $REBAS publish || echo "[warn] 今日刊自愈失败，翻牌将展示最近完整期次"
    $REBAS render                       # 翻牌：昨天备好的刊此刻上线（零 token）
    if [ -n "${DEPLOY_CMD:-}" ]; then $DEPLOY_CMD; fi
    $REBAS publish --date "$TOMORROW" --boards academic,art
    ;;
  2) $REBAS publish --date "$TOMORROW" --boards academic,art,repos,data ;;
     # 累积带上批1板块：批1若因额度耗尽/失败没做完，这里顺延重试（做过的幂等跳过，
     # 空板块还能吃到本批新采集的候选）
  3) $REBAS publish --date "$TOMORROW" --boards academic,art,repos,data,quant ;;
  4|5)
    # 批4=全板块收尾：科技+商业+扫尾，推进状态；--refill=补充轮，
    #   前三批备的板块若选题<refill_min_topics，用白天新采集的候选补选（选题够则不动）
    # 批5=兜底收尾（批4的重试）：批4完成时状态已 rendered，全阶段跳过零 token 空转；
    #   批4半途而废（额度耗尽等）时从状态断点补完剩余板块/稿件并推进状态
    $REBAS publish --date "$TOMORROW" --refill
    # 收尾批也重建+部署当日站点（内容受发布闸门保护不变）：白天同步的前端改动
    # （版式/分享键等）当天可见，不用等次日批1翻牌（2026-07-07，分享键实际踩到）
    $REBAS render
    if [ -n "${DEPLOY_CMD:-}" ]; then $DEPLOY_CMD; fi
    ;;
  *) echo "未知批次 $BATCH"; exit 1 ;;
esac

if [ -n "${HEALTHCHECK_URL:-}" ]; then
  curl -fsS -m 10 "${HEALTHCHECK_URL}${BATCH}" >/dev/null || true
fi
echo "=== batch $BATCH done $(date '+%F %T %Z') ==="
