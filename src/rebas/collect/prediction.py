"""预测市场采集（2026-08-27）：Polymarket 热点盘 + Kalshi 定点宏观盘。

定位：赔率=真金押注的群体预测（类民调信号），盘口剧烈异动=有事发生（趋势事件）。
- polymarket_events：gamma API 按 tag 流（politics/economy）取 24h 交易量榜，
  发现热点事件；tag 黑名单滤体育/娱乐赌局，交易量下限滤长尾。
- kalshi_events：公开端点无热度排序（order_by 被静默忽略，2026-08-27 实测）→
  走定点系列（FOMC 决议/CPI/衰退等，series_ticker 一源一系列，同 openalex_journal
  先例）；自带 previous_price 但不用——环比/异动统一走库内基线（db._TREND_ABS_KEYS）。

条目语义（与榜单类同构 + 两个特有机制，均在 db/runner 层）：
- published_at=None 走 fetched_at 窗口；revive 14 天；
- 摘要是赔率快照（带"截至"时间戳）→ REFRESH_SUMMARY 类型 merge 时整体刷新，
  写作期拿到的才是新盘口；
- pm_prob 进 _TREND_ABS_KEYS：merge 算百分点变动 pm_move_pp，|异动|≥REARM 阈值时
  已处理条目复位回候选池（盘口剧变=新事件，须重过粗筛/主编）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from rebas.collect.base import canonicalize_url, content_hash
from rebas.config import Source
from rebas.models import RawItem

# 体育/娱乐赌局与价格赌局不是新闻事件（politics 流里实测混进"马斯克发推数"）
_PM_TAG_BLOCKLIST = {"sports", "pop-culture", "tweets-markets", "celebrity",
                     "esports", "nfl", "nba", "mlb", "soccer", "crypto-prices"}
_PM_MIN_VOL24 = 20_000        # 美元；tag 流按 volume24hr 降序，长尾薄盘噪声截掉
_KALSHI_MIN_VOL24 = 200       # 定点系列出刊期之间交易稀薄，阈值只挡死盘


def _snapshot_ts() -> str:
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")


def parse_polymarket_events(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    events = json.loads(data)
    items: list[RawItem] = []
    skipped = 0
    for e in events:
        tags = {t.get("slug") for t in e.get("tags") or [] if t.get("slug")}
        vol24 = float(e.get("volume24hr") or 0)
        if tags & _PM_TAG_BLOCKLIST or vol24 < _PM_MIN_VOL24 or not e.get("slug"):
            skipped += 1
            continue
        markets = e.get("markets") or []
        prob = None
        lead_line = ""
        try:
            if len(markets) > 1:
                # 多选项事件（选举/决议）：每个 market 是一个选项、Yes 价=该选项概率，
                # 领跑者=Yes 价最高的选项。不能按成交量选"主市场"——近清算的杂项市场
                # （"会不会降 50bp？No 99%"）成交量常最大，pm_prob 会失真成 99/100
                # （2026-08-27 实测 Fed/巴西选举双双中招）
                def _yes(m) -> float:
                    prices = json.loads(m.get("outcomePrices") or "[0]")
                    return float(prices[0] or 0)

                top2 = sorted(markets, key=_yes, reverse=True)[:2]
                labels = [f"{(m.get('groupItemTitle') or m.get('question') or '?').strip()}"
                          f" {round(_yes(m) * 100)}%" for m in top2]
                prob = round(_yes(top2[0]) * 100)
                lead_line = "领跑 " + "、".join(labels)
            elif markets:
                m = markets[0]
                outcomes = json.loads(m.get("outcomes") or "[]")
                prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                top = max(range(len(prices)), key=prices.__getitem__)
                prob = round(prices[top] * 100)
                q = (m.get("question") or "").strip()
                lead_line = f"{q} — {outcomes[top]} {prob}%" if q else f"{outcomes[top]} {prob}%"
        except (ValueError, IndexError, TypeError):
            pass
        signals: dict = {"pm_vol24_k": round(vol24 / 1000)}
        if prob is not None:
            signals["pm_prob"] = prob
        end = (e.get("endDate") or "")[:10]
        parts = [p for p in (lead_line, f"24h 交易 ${signals['pm_vol24_k']}k",
                             f"{end} 截" if end else "") if p]
        url = f"https://polymarket.com/event/{e['slug']}"
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            url=url,
            url_canonical=canonicalize_url(url),
            title=(e.get("title") or e["slug"]).strip(),
            published_at=None,        # 盘口是持续状态非发稿事件：走 fetched_at 窗口
            summary=f"预测市场盘口（截至 {_snapshot_ts()}）：" + "；".join(parts),
            content_hash=content_hash(e["slug"]),
            image_url=e.get("image"),
            signals=signals,
        ))
    return items, skipped


def parse_kalshi_events(source: Source, data: bytes, **_) -> tuple[list[RawItem], int]:
    obj = json.loads(data)
    items: list[RawItem] = []
    skipped = 0
    for e in obj.get("events") or []:
        markets = [m for m in e.get("markets") or [] if m.get("status") in (None, "active", "open")]
        vol24 = sum(float(m.get("volume_24h_fp") or 0) for m in markets)
        if not markets or vol24 < _KALSHI_MIN_VOL24:
            skipped += 1
            continue
        lead = max(markets, key=lambda m: (float(m.get("volume_24h_fp") or 0),
                                           float(m.get("open_interest_fp") or 0)))
        prob = None
        try:
            prob = round(float(lead.get("last_price_dollars")) * 100)
        except (TypeError, ValueError):
            pass
        # 摘要列概率最高的 3 档（Fed 决议：maintain 69% / cut25 1% …）
        def _price(m) -> float:
            try:
                return float(m.get("last_price_dollars") or 0)
            except ValueError:
                return 0.0

        top3 = sorted(markets, key=_price, reverse=True)[:3]
        ladder = "、".join(
            f"{(m.get('yes_sub_title') or m.get('title') or '?').strip()} "
            f"{round(_price(m) * 100)}%" for m in top3)
        signals: dict = {"pm_vol24_k": round(vol24 / 1000)}
        if prob is not None:
            signals["pm_prob"] = prob
        title = (e.get("title") or e["event_ticker"]).strip()
        if e.get("sub_title"):
            title = f"{title}（{e['sub_title']}）"
        url = f"https://kalshi.com/markets/{e['event_ticker']}"
        items.append(RawItem(
            source_id=source.id,
            board=source.board,
            url=url,
            url_canonical=canonicalize_url(url),
            title=title,
            published_at=None,
            summary=f"预测市场盘口（截至 {_snapshot_ts()}）：{ladder}；24h 交易 ${signals['pm_vol24_k']}k",
            content_hash=content_hash(e["event_ticker"]),
            signals=signals,
        ))
    return items, skipped
