"""rebas_daily 管理后台（FastAPI）。

跑在 VPS 回环地址（默认 127.0.0.1:8787，零入站端口原则不破）：
  过渡期访问：ssh -L 8787:127.0.0.1:8787 root@<vps> → http://localhost:8787
  正式暴露：Cloudflare Tunnel 指 localhost:8787（见 docs/OPERATIONS.md）

功能：备稿状态监控 / 板块画像与出刊参数在线编辑 / 报道点赞点踩（反馈信号池，
供后续选题加权算法用——先攒数据）。

注意：画像/参数改的是 **VPS 上的 config 文件**，本地 vps_sync 会覆盖——
改完记得 scripts/vps_pull_config.sh 拉回本地入 git。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import tomlkit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from rebas import db
from rebas.admin import auth
from rebas.agents.stages import _window_clause
from rebas.collect.base import utcnow_iso
from rebas.config import CONFIG_DIR, load_config, load_profile, load_sources

app = FastAPI(title="rebas_daily admin", docs_url=None, redoc_url=None, openapi_url=None)
STATIC_DIR = Path(__file__).parent / "static"

# 出刊参数白名单：key → (最小值, 最大值)。config.toml 只放行这些键，防误改
SETTING_BOUNDS = {
    "window_hours": (24, 240),
    "paper_settle_hours": (0, 168),
    "screen_min_score": (1, 10),
    "editor_top": (10, 200),
    "feature_cap": (1, 8),
    "brief_length": (100, 800),
    "site_keep_days": (1, 60),
    "paper_fulltext_max_chars": (0, 200_000),
    "paper_deepread_length": (0, 4000),
    "refill_min_topics": (0, 12),
    "research_facts_max": (0, 10),
}


def _conn():
    return db.init_db(load_config().db_path)


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + rename 原子落盘——管线随时可能并发读 config，不能让它读到半截文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _user(request: Request) -> str:
    tok = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    email = auth.verify_token(tok) if tok else None
    if not email:
        raise HTTPException(401, "未登录或凭证已过期")
    return email


# ---------- 登录 ----------

class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/login")
def login(body: LoginIn):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM admin_users WHERE email=?",
                           (body.email.strip().lower(),)).fetchone()
    finally:
        conn.close()
    if not row or not auth.verify_password(body.password, row["pw_salt"], row["pw_hash"]):
        time.sleep(0.8)   # 拖慢在线爆破
        raise HTTPException(401, "账号或密码不对")
    return {"token": auth.issue_token(row["email"]), "email": row["email"]}


# ---------- 备稿状态 ----------

@app.get("/api/status")
def status(request: Request):
    _user(request)
    conf = load_config()
    conn = _conn()
    try:
        issues = [dict(r) for r in conn.execute(
            "SELECT issue_date, status, updated_at FROM issues"
            " ORDER BY issue_date DESC LIMIT 3")]
        for it in issues:
            it["topics"] = conn.execute(
                "SELECT count(*) FROM topics WHERE issue_date=?",
                (it["issue_date"],)).fetchone()[0]
            it["articles"] = conn.execute(
                "SELECT count(*) FROM articles a JOIN topics t ON a.topic_id=t.id"
                " WHERE t.issue_date=?", (it["issue_date"],)).fetchone()[0]

        prep_date = issues[0]["issue_date"] if issues else None
        clause, params = _window_clause(conf)
        boards = []
        for b in conf.publish_boards:
            row = conn.execute(
                "SELECT sum(decision='feature') f, sum(decision='brief') br,"
                " count(a.id) a FROM topics t LEFT JOIN articles a ON a.topic_id=t.id"
                " WHERE t.issue_date=? AND t.board=?", (prep_date, b)).fetchone()
            cand = conn.execute(
                f"SELECT sum(status='new') n, sum(status='screened') s FROM raw_items"
                f" WHERE board=? AND status IN ('new','screened') AND {clause}",
                [b, *params]).fetchone()
            boards.append({
                "board": b, "name": load_profile(b).name,
                "features": row["f"] or 0, "briefs": row["br"] or 0,
                "articles": row["a"] or 0,
                "cand_new": cand["n"] or 0, "cand_screened": cand["s"] or 0,
            })

        last_new = {r["source_id"]: r["m"] for r in conn.execute(
            "SELECT source_id, max(fetched_at) m FROM raw_items GROUP BY source_id")}
        fstate = {r["source_id"]: dict(r) for r in conn.execute(
            "SELECT source_id, last_fetch_at, last_status FROM fetch_state")}
        sources = [{
            "id": s.id, "board": s.board, "name": s.name,
            "last_new_at": last_new.get(s.id),
            "last_fetch_at": fstate.get(s.id, {}).get("last_fetch_at"),
            "last_status": fstate.get(s.id, {}).get("last_status"),
        } for s in load_sources(enabled_only=True)]
    finally:
        conn.close()
    return {"now": utcnow_iso(), "timezone": conf.timezone,
            "issues": issues, "prep_date": prep_date,
            "boards": boards, "sources": sources}


@app.get("/api/log")
def batch_log(request: Request, lines: int = 120):
    _user(request)
    path = load_config().data_dir / "logs" / "batch.log"
    if not path.exists():
        return {"log": "（暂无批次日志）"}
    n = max(1, min(lines, 800))
    tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    return {"log": "\n".join(tail)}


# ---------- 板块画像 ----------

class InterestIn(BaseModel):
    name: str
    weight: int = Field(ge=1, le=5)
    keywords: list[str]


class ProfileIn(BaseModel):
    name: str
    interests: list[InterestIn]
    low_priority: list[str] = []
    blocklist: list[str] = []
    reader_assumed: str = ""
    reader_explain: str = ""


def validate_profile_payload(p: ProfileIn) -> list[str]:
    """→ 错误列表（空 = 合法）。画像是粗筛与主编的地基，改坏会静默毁池，从严校验。"""
    errs = []
    if not p.name.strip():
        errs.append("板块名不能为空")
    if not p.interests:
        errs.append("至少要有一个兴趣方向")
    for i, it in enumerate(p.interests, 1):
        if not it.name.strip():
            errs.append(f"兴趣 #{i} 名称为空")
        kws = [k.strip() for k in it.keywords if k.strip()]
        if not kws:
            errs.append(f"兴趣「{it.name or i}」至少要有一个关键词")
    return errs


def apply_profile_to_doc(doc: tomlkit.TOMLDocument, p: ProfileIn) -> None:
    """把画像编辑落进 tomlkit 文档（尽量保留文件头注释；interest 数组整体重建）。"""
    doc["board"]["name"] = p.name.strip()
    aot = tomlkit.aot()
    for it in p.interests:
        t = tomlkit.table()
        t["name"] = it.name.strip()
        t["weight"] = it.weight
        t["keywords"] = [k.strip() for k in it.keywords if k.strip()]
        aot.append(t)
    doc["interest"] = aot
    for key, vals in (("low_priority", p.low_priority), ("blocklist", p.blocklist)):
        if key not in doc:
            doc[key] = tomlkit.table()
        doc[key]["keywords"] = [k.strip() for k in vals if k.strip()]
    if "reader" not in doc:
        doc["reader"] = tomlkit.table()
    doc["reader"]["assumed"] = p.reader_assumed.strip()
    doc["reader"]["explain"] = p.reader_explain.strip()


def _profile_path(board: str) -> Path:
    conf = load_config()
    if board not in conf.publish_boards:
        raise HTTPException(404, f"未知板块 {board}")
    return CONFIG_DIR / "profiles" / f"{board}.toml"


@app.get("/api/profiles")
def profiles(request: Request):
    _user(request)
    conf = load_config()
    out = []
    for b in conf.publish_boards:
        p = load_profile(b)
        out.append({
            "board": b, "name": p.name,
            "interests": [{"name": i.name, "weight": i.weight,
                           "keywords": list(i.keywords)} for i in p.interests],
            "low_priority": list(p.low_priority), "blocklist": list(p.blocklist),
            "reader_assumed": p.reader_assumed, "reader_explain": p.reader_explain,
        })
    return {"profiles": out}


@app.put("/api/profiles/{board}")
def put_profile(board: str, body: ProfileIn, request: Request):
    _user(request)
    errs = validate_profile_payload(body)
    if errs:
        raise HTTPException(422, "；".join(errs))
    path = _profile_path(board)
    original = path.read_text(encoding="utf-8")
    doc = tomlkit.parse(original)
    apply_profile_to_doc(doc, body)
    _atomic_write(path, tomlkit.dumps(doc))
    try:
        load_profile(board)   # 回读校验，坏了立即回滚
    except Exception as e:  # noqa: BLE001
        _atomic_write(path, original)
        raise HTTPException(500, f"写入后解析失败已回滚: {e}") from e
    return {"ok": True}


# ---------- 出刊参数 ----------

@app.get("/api/settings")
def settings(request: Request):
    _user(request)
    conf = load_config()
    return {"settings": {k: getattr(conf, k) for k in SETTING_BOUNDS},
            "bounds": SETTING_BOUNDS}


@app.put("/api/settings")
def put_settings(body: dict, request: Request):
    _user(request)
    updates = {}
    for k, v in body.items():
        if k not in SETTING_BOUNDS:
            raise HTTPException(422, f"不放行的参数 {k}")
        lo, hi = SETTING_BOUNDS[k]
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{k} 必须是整数") from None
        if not lo <= v <= hi:
            raise HTTPException(422, f"{k} 超出范围 [{lo}, {hi}]")
        updates[k] = v
    if not updates:
        return {"ok": True, "changed": 0}
    path = CONFIG_DIR / "config.toml"
    original = path.read_text(encoding="utf-8")
    doc = tomlkit.parse(original)
    for k, v in updates.items():
        doc["publish"][k] = v
    _atomic_write(path, tomlkit.dumps(doc))
    try:
        load_config()
    except Exception as e:  # noqa: BLE001
        _atomic_write(path, original)
        raise HTTPException(500, f"写入后解析失败已回滚: {e}") from e
    return {"ok": True, "changed": len(updates)}


# ---------- 报道反馈 ----------

@app.get("/api/issues")
def list_issues(request: Request):
    _user(request)
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT issue_date, status FROM issues ORDER BY issue_date DESC LIMIT 14")]
    finally:
        conn.close()
    return {"issues": rows}


@app.get("/api/topics")
def topics_of(request: Request, date: str):
    _user(request)
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.id, t.board, t.decision, t.slot, t.title,"
            " a.card_summary, f.vote FROM topics t"
            " LEFT JOIN articles a ON a.topic_id=t.id"
            " LEFT JOIN feedback f ON f.topic_id=t.id"
            " WHERE t.issue_date=? ORDER BY t.board,"
            " CASE t.slot WHEN 'headline' THEN 0 ELSE 1 END,"
            " t.decision='brief', t.id", (date,))]
    finally:
        conn.close()
    return {"topics": rows}


class VoteIn(BaseModel):
    topic_id: int
    vote: int = Field(ge=-1, le=1)   # 1 赞 | -1 踩 | 0 取消


@app.post("/api/feedback")
def vote(body: VoteIn, request: Request):
    _user(request)
    conn = _conn()
    try:
        if not conn.execute("SELECT 1 FROM topics WHERE id=?",
                            (body.topic_id,)).fetchone():
            raise HTTPException(404, "选题不存在")
        if body.vote == 0:
            conn.execute("DELETE FROM feedback WHERE topic_id=?", (body.topic_id,))
        else:
            conn.execute(
                "INSERT INTO feedback (topic_id, vote, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(topic_id) DO UPDATE SET vote=excluded.vote,"
                " updated_at=excluded.updated_at",
                (body.topic_id, body.vote, utcnow_iso()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/feedback/summary")
def feedback_summary(request: Request):
    _user(request)
    conn = _conn()
    try:
        agg = [dict(r) for r in conn.execute(
            "SELECT t.board, sum(f.vote=1) up, sum(f.vote=-1) down FROM feedback f"
            " JOIN topics t ON t.id=f.topic_id GROUP BY t.board ORDER BY t.board")]
        recent = [dict(r) for r in conn.execute(
            "SELECT t.issue_date, t.board, t.title, f.vote FROM feedback f"
            " JOIN topics t ON t.id=f.topic_id"
            " ORDER BY f.updated_at DESC LIMIT 50")]
    finally:
        conn.close()
    return {"by_board": agg, "recent": recent}


# ---------- 页面 ----------

# PWA manifest：图标复用主站线上资源（品牌文件不入 git，经 rebasdaily.com 分发）
_MANIFEST = {
    "name": "Rebas Daily 编辑部",
    "short_name": "Rebas 编辑部",
    "description": "备稿监控 · 画像与参数 · 报道反馈",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f1f1f0",
    "theme_color": "#f1f1f0",
    "icons": [
        {"src": "https://rebasdaily.com/icons/icon-192.png",
         "sizes": "192x192", "type": "image/png"},
        {"src": "https://rebasdaily.com/icons/icon-512.png",
         "sizes": "512x512", "type": "image/png"},
        {"src": "https://rebasdaily.com/icons/maskable-512.png",
         "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(_MANIFEST, media_type="application/manifest+json")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "admin.html")


@app.exception_handler(Exception)
async def unhandled(request, exc):  # noqa: ANN001 —— 兜底防泄栈
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
