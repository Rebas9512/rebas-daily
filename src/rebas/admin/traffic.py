"""站点流量监控：信标摄入 / 聚合查询 / CF zone 对照拉取（2026-07-23）。

真人与爬虫的分界线 = 是否执行 JS：主站 Layout 里的 sendBeacon 只有真浏览器会发，
CF zone 面板的 UV 按 IP 算、爬虫全在内（2026-07 实测人均 3.5 请求/深夜无低谷，
大头是爬虫）。两层数据对照，差值即机器流量。

- 信标：主站内联 JS → POST https://t.rebasdaily.com/t（Tunnel 公开主机名，与 admin
  同一个 FastAPI；无鉴权，靠限速 + UA 黑名单 + 载荷校验兜滥用）。
- 隐私口径：不落原始 IP/UA——visitor = sha256(日盐+IP+UA) 前 16 hex，日盐换天即换，
  跨天不可关联；referrer 只留站外 host；无 cookie。
- zone 层：GraphQL httpRequests1dGroups 日汇总（真人+爬虫），需专用 token
  CLOUDFLARE_ANALYTICS_TOKEN（Zone.Analytics:Read + Zone.Zone:Read——
  部署用的 Pages:Edit token 权限不够，2026-07-23 实测 zones 列表为空）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from rebas.admin import auth
from rebas.config import PROJECT_ROOT

ZONE_NAME = "rebasdaily.com"

# 执行了 JS 仍要拦的：无头浏览器、预渲染服务、监控探针（真爬虫大多到不了这层）
_BOT_RE = re.compile(
    r"bot|spider|crawl|slurp|headless|phantom|selenium|puppeteer|playwright"
    r"|lighthouse|pagespeed|pingdom|uptime|monitor|prerender|preview|scrapy"
    r"|python|curl|wget|httpx|aiohttp|go-http|okhttp|java/|libwww|node-fetch",
    re.IGNORECASE)

_MOBILE_RE = re.compile(r"Mobi|Android|iPhone|iPad", re.IGNORECASE)


# ---------- 信标摄入 ----------

class RateLimiter:
    """每 IP 滑动窗限速（进程内存态，admin 单 worker 够用）。"""

    def __init__(self, max_hits: int = 120, window_s: int = 600):
        self.max_hits, self.window_s = max_hits, window_s
        self._hits: dict[str, list] = {}   # ip → [窗口起点, 计数]

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        if len(self._hits) > 10_000:       # 防伪造海量 IP 撑爆内存
            self._hits.clear()
        slot = self._hits.get(ip)
        if slot is None or now - slot[0] > self.window_s:
            self._hits[ip] = [now, 1]
            return True
        slot[1] += 1
        return slot[1] <= self.max_hits


limiter = RateLimiter()


def visitor_hash(ip: str, ua: str, date: str) -> str:
    """日盐哈希：同日同人稳定（UV 去重），跨天不可关联。盐复用 admin JWT 密钥派生。"""
    salt = hashlib.sha256(f"{auth._secret()}|{date}".encode()).hexdigest()
    return hashlib.sha256(f"{salt}|{ip}|{ua}".encode()).hexdigest()[:16]


def _norm_path(p: str) -> str | None:
    """→ 规范路径；不合法返回 None。去 query/fragment/尾斜杠/.html。"""
    if not isinstance(p, str) or not p.startswith("/") or len(p) > 200:
        return None
    p = p.split("?")[0].split("#")[0]
    if p.endswith(".html"):
        p = p[:-5]
    p = p.rstrip("/") or "/"
    if p == "/index":
        p = "/"
    return p


def _norm_referrer(r: str) -> str | None:
    """→ 站外来路 host；站内跳转/空值/坏值返回 None。"""
    if not isinstance(r, str) or not r.strip():
        return None
    try:
        host = urllib.parse.urlsplit(r.strip()[:500]).hostname or ""
    except ValueError:
        return None
    host = host.lower()
    if not host or host == ZONE_NAME or host.endswith("." + ZONE_NAME):
        return None
    return host[:100]


def ingest(conn: sqlite3.Connection, tz_name: str, *, ip: str, ua: str,
           country: str | None, raw_body: bytes) -> bool:
    """校验并落一条 page_view。返回是否入库（bot/坏载荷/限速静默丢弃）。"""
    if not ip or not ua or _BOT_RE.search(ua):
        return False
    if not limiter.allow(ip):
        return False
    if len(raw_body) > 2048:
        return False
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    path = _norm_path(payload.get("p"))
    if path is None:
        return False
    title = payload.get("t")
    title = title.strip()[:160] if isinstance(title, str) and title.strip() else None
    now_utc = datetime.now(timezone.utc)
    local = now_utc.astimezone(ZoneInfo(tz_name))
    date = local.strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO page_views (ts, date, hour, visitor, path, title, referrer,"
        " country, device) VALUES (?,?,?,?,?,?,?,?,?)",
        (now_utc.isoformat(timespec="seconds"), date, local.hour,
         visitor_hash(ip, ua, date), path, title,
         _norm_referrer(payload.get("r", "")),
         (country or "").upper()[:2] or None,
         "mobile" if _MOBILE_RE.search(ua) else "desktop"))
    conn.commit()
    return True


# ---------- 聚合查询（admin「流量」页） ----------

def _section(path: str) -> str:
    """路径 → 栏目：/ = home，/topic-* = 文章页，/issue-* = 往期，其余首段（板块 tab 等）。"""
    if path == "/":
        return "home"
    seg = path.split("/")[1]
    if seg.startswith("topic-"):
        return "topic"
    if seg.startswith("issue-"):
        return "issue"
    return seg


def summary(conn: sqlite3.Connection, tz_name: str, days: int = 30) -> dict:
    """监控页一次性取数：日趋势（真实 vs zone）、今日牌面、时段/板块/文章/来路/国家。"""
    days = max(7, min(days, 90))
    local_today = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()
    d0 = (local_today - timedelta(days=days - 1)).isoformat()

    real = {r["date"]: dict(r) for r in conn.execute(
        "SELECT date, count(DISTINCT visitor) uv, count(*) pv FROM page_views"
        " WHERE date >= ? GROUP BY date", (d0,))}
    # 保留窗外的日子（days=90 且已 rollup 时）从永久聚合补
    for r in conn.execute(
            "SELECT date, uv, pv FROM traffic_daily WHERE date >= ?", (d0,)):
        real.setdefault(r["date"], dict(r))
    zone = {r["date"]: dict(r) for r in conn.execute(
        "SELECT date, requests, uniques FROM traffic_zone_daily WHERE date >= ?", (d0,))}
    daily = []
    for i in range(days):
        d = (local_today - timedelta(days=days - 1 - i)).isoformat()
        z = zone.get(d, {})
        daily.append({"date": d, "uv": real.get(d, {}).get("uv", 0),
                      "pv": real.get(d, {}).get("pv", 0),
                      "zone_uv": z.get("uniques"), "zone_req": z.get("requests")})

    today = daily[-1] if daily else {"uv": 0, "pv": 0}
    week_uv = conn.execute(
        "SELECT count(DISTINCT date || visitor) FROM page_views WHERE date >= ?",
        ((local_today - timedelta(days=6)).isoformat(),)).fetchone()[0]

    d7 = (local_today - timedelta(days=6)).isoformat()
    hours = [0] * 24
    for r in conn.execute(
            "SELECT hour, count(*) n FROM page_views WHERE date >= ? GROUP BY hour", (d7,)):
        hours[r["hour"]] = r["n"]

    sections: dict[str, int] = {}
    for r in conn.execute(
            "SELECT path, count(*) n FROM page_views WHERE date >= ? GROUP BY path", (d7,)):
        s = _section(r["path"])
        sections[s] = sections.get(s, 0) + r["n"]

    pages = [dict(r) for r in conn.execute(
        "SELECT coalesce(title, path) title, count(*) pv, count(DISTINCT visitor) uv"
        " FROM page_views WHERE date >= ? AND path LIKE '/topic-%'"
        " GROUP BY coalesce(title, path) ORDER BY pv DESC LIMIT 10", (d7,))]
    referrers = [dict(r) for r in conn.execute(
        "SELECT referrer, count(*) n FROM page_views WHERE date >= ?"
        " AND referrer IS NOT NULL GROUP BY referrer ORDER BY n DESC LIMIT 10", (d7,))]
    countries = [dict(r) for r in conn.execute(
        "SELECT country, count(DISTINCT visitor) n FROM page_views WHERE date >= ?"
        " AND country IS NOT NULL GROUP BY country ORDER BY n DESC LIMIT 10", (d7,))]
    devices = {r["device"]: r["n"] for r in conn.execute(
        "SELECT device, count(DISTINCT visitor) n FROM page_views WHERE date >= ?"
        " GROUP BY device", (d7,))}

    # 真实占比用昨日（zone 拉取是 T+1，今日 zone 行还没有）
    yd = daily[-2] if len(daily) >= 2 else {}
    ratio = (round(yd["uv"] / yd["zone_uv"] * 100, 1)
             if yd.get("zone_uv") and yd.get("uv") is not None else None)

    return {"days": days, "daily": daily,
            "today": {"uv": today["uv"], "pv": today["pv"], "week_uv": week_uv,
                      "ppv": round(today["pv"] / today["uv"], 1) if today["uv"] else 0},
            "yesterday": {"uv": yd.get("uv", 0), "zone_uv": yd.get("zone_uv"),
                          "real_ratio": ratio},
            "hours": hours, "sections": sections, "pages": pages,
            "referrers": referrers, "countries": countries, "devices": devices,
            "has_zone": bool(zone)}


# ---------- CF zone 拉取（traffic-pull，cron 批 1 顺带） ----------

def _env(key: str) -> str | None:
    """环境变量优先，缺了读 .secrets/.env（cron 不 source 它，CLI 自己兜）。"""
    import os
    if os.environ.get(key):
        return os.environ[key]
    envf = PROJECT_ROOT / ".secrets" / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


def _cf_api(url: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def zone_pull(conn: sqlite3.Connection, days: int = 3) -> int:
    """拉最近 N 天 zone 日汇总 upsert 进 traffic_zone_daily。→ 写入行数。

    无 token 返回 -1（调用方静默跳过——token 是可选增强层，没配不算错）。
    """
    token = _env("CLOUDFLARE_ANALYTICS_TOKEN")
    if not token:
        return -1
    zones = _cf_api(f"https://api.cloudflare.com/client/v4/zones?name={ZONE_NAME}",
                    token).get("result") or []
    if not zones:
        raise RuntimeError(f"token 看不到 zone {ZONE_NAME}（缺 Zone:Read 权限？）")
    ztag = zones[0]["id"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    q = """query($zone: String!, $since: String!) {
      viewer { zones(filter: {zoneTag: $zone}) {
        httpRequests1dGroups(limit: 10, filter: {date_geq: $since}) {
          dimensions { date }
          sum { requests cachedRequests bytes }
          uniq { uniques } } } } }"""
    data = _cf_api("https://api.cloudflare.com/client/v4/graphql", token,
                   {"query": q, "variables": {"zone": ztag, "since": since}})
    if data.get("errors"):
        raise RuntimeError(f"GraphQL: {data['errors'][0].get('message')}")
    groups = data["data"]["viewer"]["zones"][0]["httpRequests1dGroups"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for g in groups:
        conn.execute(
            "INSERT INTO traffic_zone_daily (date, requests, uniques, cached_requests,"
            " bytes, fetched_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(date) DO UPDATE SET requests=excluded.requests,"
            " uniques=excluded.uniques, cached_requests=excluded.cached_requests,"
            " bytes=excluded.bytes, fetched_at=excluded.fetched_at",
            (g["dimensions"]["date"], g["sum"]["requests"], g["uniq"]["uniques"],
             g["sum"]["cachedRequests"], g["sum"]["bytes"], now))
    conn.commit()
    return len(groups)
