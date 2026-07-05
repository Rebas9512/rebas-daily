#!/usr/bin/env bash
# rebas_daily — Codex CLI 可用性验证
#
# 本项目使用独立的 CODEX_HOME（<项目根>/.codex/）保存凭证，
# 与 VSCode ChatGPT 扩展的 ~/.codex 完全隔离。该目录已被 .gitignore 排除。
#
# 用法:
#   scripts/verify_codex.sh login    # 浏览器 OAuth 登录（需要人工操作，登录时选个人 workspace）
#   scripts/verify_codex.sh status   # 查看登录账号 / 订阅计划 / 有效期
#   scripts/verify_codex.sh test     # 无头模式冒烟测试（JSON 选题打分任务）
#   scripts/verify_codex.sh          # status + test
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CODEX_HOME="$PROJECT_DIR/.codex"
mkdir -p "$CODEX_HOME"
chmod 700 "$CODEX_HOME"

find_codex() {
    if command -v codex >/dev/null 2>&1; then
        command -v codex
        return
    fi
    # 兜底：VSCode ChatGPT 扩展自带的二进制，取最新版本
    ls -1d "$HOME"/.vscode/extensions/openai.chatgpt-*/bin/linux-x86_64/codex 2>/dev/null | sort -V | tail -1
}

CODEX_BIN="$(find_codex)"
if [ -z "${CODEX_BIN:-}" ]; then
    echo "✗ 找不到 codex 二进制（PATH 和 VSCode 扩展目录都没有）" >&2
    exit 1
fi
echo "codex 二进制: $CODEX_BIN ($("$CODEX_BIN" --version))"
echo "CODEX_HOME:   $CODEX_HOME"
echo

cmd_login() {
    echo "即将打开浏览器进行 OAuth 登录。"
    echo "★ 登录时请选择【新的个人订阅】所在的 workspace，不要选过期的 Team。"
    exec "$CODEX_BIN" login
}

cmd_status() {
    echo "== 登录状态 =="
    "$CODEX_BIN" login status || { echo "→ 未登录，请先运行: scripts/verify_codex.sh login"; return 1; }
    python3 - "$CODEX_HOME/auth.json" <<'PYEOF'
import json, base64, sys, datetime
try:
    auth = json.load(open(sys.argv[1]))
except FileNotFoundError:
    sys.exit("✗ auth.json 不存在，请先登录")
idt = (auth.get("tokens") or {}).get("id_token")
if not idt:
    sys.exit("✗ auth.json 里没有 id_token")
payload = idt.split(".")[1]
claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
info = claims.get("https://api.openai.com/auth", {})
plan = info.get("chatgpt_plan_type")
until = info.get("chatgpt_subscription_active_until")
print(f"账号:     {claims.get('email')}")
print(f"计划类型: {plan}")
print(f"有效期至: {until}")
if until:
    exp = datetime.datetime.fromisoformat(until)
    if exp < datetime.datetime.now(datetime.timezone.utc):
        sys.exit(f"✗ 订阅已过期（{until}），登录的不是有效订阅的 workspace")
print("✓ 订阅状态正常")
PYEOF
}

cmd_test() {
    echo "== 无头模式冒烟测试（迷你选题打分，要求纯 JSON 输出） =="
    local out="$CODEX_HOME/smoke_test_output.txt"
    rm -f "$out"
    local start end
    start=$(date +%s)
    "$CODEX_BIN" exec --skip-git-repo-check -s read-only \
        --output-last-message "$out" \
        '你是日刊选题助手。对下面3条信息按"AI/ML研究价值"打分(0-10)，只输出JSON，不要输出任何其他文字。格式: {"scores":[{"id":1,"score":N,"reason":"一句话"},...]}
条目:
1. "Scaling Laws for Sparse Mixture-of-Experts Models" (arXiv cs.LG)
2. "10 Best Coffee Shops in Berlin" (lifestyle blog)
3. "DeepMind announces new protein folding breakthrough" (official blog)' \
        > "$CODEX_HOME/smoke_test_log.txt" 2>&1 || {
            echo "✗ codex exec 调用失败，日志尾部:"
            tail -8 "$CODEX_HOME/smoke_test_log.txt"
            return 1
        }
    end=$(date +%s)
    echo "耗时: $((end - start))s"
    python3 - "$out" <<'PYEOF'
import json, re, sys
raw = open(sys.argv[1]).read().strip()
m = re.search(r"\{.*\}", raw, re.S)
if not m:
    sys.exit(f"✗ 输出中找不到 JSON:\n{raw[:500]}")
try:
    data = json.loads(m.group(0))
except json.JSONDecodeError as e:
    sys.exit(f"✗ JSON 解析失败: {e}\n{raw[:500]}")
scores = data.get("scores", [])
for s in scores:
    print(f"  条目{s.get('id')}: {s.get('score')}分 — {s.get('reason')}")
ok = len(scores) == 3 and all(isinstance(s.get("score"), (int, float)) for s in scores)
print("✓ 冒烟测试通过：JSON 结构合法、3 条评分齐全" if ok else "✗ JSON 可解析但结构不符合预期")
sys.exit(0 if ok else 1)
PYEOF
}

case "${1:-all}" in
    login)  cmd_login ;;
    status) cmd_status ;;
    test)   cmd_test ;;
    all)    cmd_status && echo && cmd_test ;;
    *)      echo "用法: $0 [login|status|test]" >&2; exit 2 ;;
esac
