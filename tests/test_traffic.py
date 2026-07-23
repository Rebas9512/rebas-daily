"""流量监控测试：信标摄入（校验/去重/过滤/限速）、滚动聚合、监控页查询、zone 拉取。"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from rebas import db as database
from rebas.admin import auth
from rebas.admin import traffic

TZ = "America/Chicago"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


def _local_date(days_ago=0):
    return (datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date()
            - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "jwt.secret")
    monkeypatch.setattr(traffic, "limiter", traffic.RateLimiter())
    c = database.init_db(tmp_path / "t.sqlite")
    yield c
    c.close()


def hit(conn, *, ip="1.2.3.4", ua=UA, country="US",
        p="/academic", t="标题", r="https://news.ycombinator.com/item?id=1"):
    return traffic.ingest(conn, TZ, ip=ip, ua=ua, country=country,
                          raw_body=json.dumps({"p": p, "t": t, "r": r}).encode())


class TestIngest:
    def test_basic_row(self, conn):
        assert hit(conn)
        row = conn.execute("SELECT * FROM page_views").fetchone()
        assert row["date"] == _local_date()
        assert row["path"] == "/academic"
        assert row["title"] == "标题"
        assert row["referrer"] == "news.ycombinator.com"
        assert row["country"] == "US"
        assert row["device"] == "desktop"
        assert 0 <= row["hour"] <= 23
        assert len(row["visitor"]) == 16

    def test_visitor_dedup_same_day(self, conn):
        hit(conn); hit(conn); hit(conn, ip="5.6.7.8")
        uv = conn.execute("SELECT count(DISTINCT visitor) FROM page_views").fetchone()[0]
        assert uv == 2

    def test_salt_changes_by_day(self):
        h1 = traffic.visitor_hash("1.2.3.4", UA, "2026-07-22")
        h2 = traffic.visitor_hash("1.2.3.4", UA, "2026-07-23")
        assert h1 != h2

    def test_mobile_device(self, conn):
        hit(conn, ua=UA_MOBILE)
        assert conn.execute("SELECT device FROM page_views").fetchone()[0] == "mobile"

    def test_bot_and_junk_dropped(self, conn):
        assert not hit(conn, ua="Mozilla/5.0 (compatible; GPTBot/1.0)")
        assert not hit(conn, ua="python-requests/2.31")
        assert not hit(conn, ua="")                      # 无 UA
        assert not hit(conn, p="not-a-path")             # 路径不以 / 开头
        assert not hit(conn, p="/x" * 150)               # 超长路径
        assert not traffic.ingest(conn, TZ, ip="1.1.1.1", ua=UA, country=None,
                                  raw_body=b"\xff not json")
        assert not traffic.ingest(conn, TZ, ip="1.1.1.1", ua=UA, country=None,
                                  raw_body=b"x" * 4096)  # 超长载荷
        assert conn.execute("SELECT count(*) FROM page_views").fetchone()[0] == 0

    def test_path_normalization(self, conn):
        hit(conn, p="/academic/?utm_source=x#frag")
        hit(conn, p="/index.html")
        paths = [r[0] for r in conn.execute("SELECT path FROM page_views ORDER BY id")]
        assert paths == ["/academic", "/"]

    def test_internal_referrer_dropped(self, conn):
        hit(conn, r="https://rebasdaily.com/quant")
        assert conn.execute("SELECT referrer FROM page_views").fetchone()[0] is None

    def test_rate_limit(self, conn, monkeypatch):
        monkeypatch.setattr(traffic, "limiter", traffic.RateLimiter(max_hits=3))
        results = [hit(conn, ip="9.9.9.9") for _ in range(5)]
        assert results == [True, True, True, False, False]


class TestRollupAndSummary:
    def seed(self, conn, date, visitor, path="/", title=None, hour=8, **kw):
        conn.execute(
            "INSERT INTO page_views (ts, date, hour, visitor, path, title, referrer,"
            " country, device) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{date}T12:00:00+00:00", date, hour, visitor, path, title,
             kw.get("referrer"), kw.get("country", "US"), kw.get("device", "desktop")))

    def test_rollup(self, conn):
        self.seed(conn, "2026-01-01", "aaaa", "/tech")
        self.seed(conn, "2026-01-01", "bbbb", "/tech", device="mobile")
        self.seed(conn, "2026-01-01", "bbbb", "/")
        recent = _local_date()
        self.seed(conn, recent, "cccc")
        n = database.traffic_rollup(conn, "2026-06-01")
        assert n == 3
        agg = conn.execute("SELECT * FROM traffic_daily WHERE date='2026-01-01'").fetchone()
        assert (agg["uv"], agg["pv"]) == (2, 3)
        assert json.loads(agg["detail"])["device"] == {"desktop": 2, "mobile": 1}
        left = [r[0] for r in conn.execute("SELECT date FROM page_views")]
        assert left == [recent]   # 保留窗内的原始行不动

    def test_summary(self, conn):
        today, yesterday = _local_date(), _local_date(1)
        self.seed(conn, today, "aaaa", "/", hour=9)
        self.seed(conn, today, "aaaa", "/topic-x-1", title="注意力还够用吗", hour=9,
                  referrer="news.ycombinator.com")
        self.seed(conn, today, "bbbb", "/quant", hour=21, country="DE")
        self.seed(conn, yesterday, "dddd", "/archive")
        conn.execute("INSERT INTO traffic_zone_daily (date, requests, uniques,"
                     " cached_requests, bytes, fetched_at) VALUES (?,?,?,?,?,?)",
                     (yesterday, 3700, 1000, 70, 2_800_000, "t"))
        s = traffic.summary(conn, TZ, days=30)
        assert len(s["daily"]) == 30
        assert s["today"] == {"uv": 2, "pv": 3, "week_uv": 3, "ppv": 1.5}
        assert s["yesterday"]["zone_uv"] == 1000
        assert s["yesterday"]["real_ratio"] == 0.1     # 1 / 1000
        assert s["hours"][9] == 2 and s["hours"][21] == 1
        assert s["sections"] == {"home": 1, "topic": 1, "quant": 1, "archive": 1}
        assert s["pages"][0]["title"] == "注意力还够用吗"
        assert s["referrers"][0]["referrer"] == "news.ycombinator.com"
        assert {c["country"] for c in s["countries"]} == {"US", "DE"}
        assert s["has_zone"]

    def test_summary_merges_rolled_up_days(self, conn):
        d = _local_date(5)
        conn.execute("INSERT INTO traffic_daily (date, uv, pv, detail) VALUES (?,?,?,?)",
                     (d, 7, 20, "{}"))
        s = traffic.summary(conn, TZ, days=7)
        assert next(x for x in s["daily"] if x["date"] == d)["uv"] == 7


class TestZonePull:
    def test_no_token_skips(self, conn, monkeypatch):
        monkeypatch.setattr(traffic, "_env", lambda k: None)
        assert traffic.zone_pull(conn) == -1

    def test_pull_and_upsert(self, conn, monkeypatch):
        monkeypatch.setattr(traffic, "_env", lambda k: "tok")
        calls = []

        def fake_api(url, token, body=None):
            calls.append(url)
            if "zones?" in url:
                return {"result": [{"id": "zzz"}]}
            return {"data": {"viewer": {"zones": [{"httpRequests1dGroups": [
                {"dimensions": {"date": "2026-07-22"},
                 "sum": {"requests": 3700, "cachedRequests": 70, "bytes": 2_800_000},
                 "uniq": {"uniques": 1040}}]}]}}}

        monkeypatch.setattr(traffic, "_cf_api", fake_api)
        assert traffic.zone_pull(conn) == 1
        assert traffic.zone_pull(conn) == 1   # 幂等 upsert
        row = conn.execute("SELECT * FROM traffic_zone_daily").fetchone()
        assert (row["date"], row["uniques"], row["requests"]) == ("2026-07-22", 1040, 3700)

    def test_zone_missing_raises(self, conn, monkeypatch):
        monkeypatch.setattr(traffic, "_env", lambda k: "tok")
        monkeypatch.setattr(traffic, "_cf_api", lambda *a, **k: {"result": []})
        with pytest.raises(RuntimeError, match="Zone:Read"):
            traffic.zone_pull(conn)
