from __future__ import annotations

import bisect
import json
import math
import statistics
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .core import SYMBOLS, classify_session
from .storage import MarketStore, bucket_start_iso, parse_iso

SHANGHAI = ZoneInfo("Asia/Shanghai")
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
NAV_HISTORY_URL = "https://api.freebacktrack.tech/api/holdings/nav-history"
MARKETS_KLINE_URL = "https://api.freebacktrack.tech/api/markets/kline"

GROUPS = [
    {"key": "all", "label": "全部", "order": 0, "codes": list(SYMBOLS)},
    {
        "key": "nasdaq-100", "label": "纳指 100", "order": 10,
        "codes": ["513870", "513390", "513300", "513110", "513100", "159941", "159696", "159660", "159659", "159632", "159513", "159501", "161130"],
    },
    {"key": "sp500", "label": "标普 500", "order": 20, "codes": ["513500", "513650", "159612", "159655"]},
    {"key": "us-specialty", "label": "美国主题", "order": 30, "codes": ["159509", "159577", "161128", "513850"]},
]

FetchJson = Callable[[str, float], dict[str, Any]]
PostJson = Callable[[str, dict[str, Any], float], dict[str, Any]]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round4(value: Any) -> float | None:
    number = _number(value)
    return round(number, 4) if number is not None else None


def _fetch_json(url: str, timeout_sec: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 market-collector/1",
        "referer": "https://quote.eastmoney.com/",
    })
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _post_json(url: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "accept": "application/json", "content-type": "application/json",
        "user-agent": "Mozilla/5.0 market-collector/1",
    })
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _shanghai_iso(value: datetime) -> str:
    return value.astimezone(SHANGHAI).replace(microsecond=0).isoformat()


def _date_epoch(value: str) -> int:
    return int(datetime.fromisoformat(value + "T00:00:00+08:00").timestamp())


def _previous_date(value: str) -> str:
    return (date.fromisoformat(value) - timedelta(days=1)).isoformat()


def _market_state() -> tuple[str, str]:
    session = classify_session(datetime.now(timezone.utc))
    current = datetime.now(SHANGHAI)
    if current.weekday() >= 5:
        return "holiday", "非交易日"
    if session == "trading":
        return "open", "A 股连续竞价"
    if session == "lunch":
        return "lunch_break", "A 股午间休市"
    if current.time() < datetime.strptime("09:30", "%H:%M").time():
        return "pre_open", "A 股盘前"
    return "closed", "A 股已收市"


class TimedCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_load(self, key: str, ttl_sec: int, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            cached = self._items.get(key)
            if cached and cached[0] > now:
                return cached[1]
        value = loader()
        with self._lock:
            self._items[key] = (now + ttl_sec, value)
        return value


class MarketDataService:
    def __init__(
        self,
        store: MarketStore,
        data_dir: str | Path,
        fetch_json: FetchJson = _fetch_json,
        post_json: PostJson = _post_json,
        timeout_sec: float = 12.0,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir)
        self.fetch_json = fetch_json
        self.post_json = post_json
        self.timeout_sec = timeout_sec
        self.cache = TimedCache()

    def _latest(self) -> dict[str, Any]:
        return json.loads((self.data_dir / "latest.json").read_text(encoding="utf-8"))

    def _latest_by_symbol(self) -> dict[str, dict[str, Any]]:
        records = {str(item.get("symbol")): item for item in self._latest().get("symbols", [])}
        try:
            otc_payload = json.loads((self.data_dir / "otc-latest.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            otc_payload = {}
        for item in otc_payload.get("items") or []:
            code = str(item.get("code") or item.get("symbol") or "")
            if code:
                records[code] = item
        return records

    def otc_latest(self) -> dict[str, Any]:
        return json.loads((self.data_dir / "otc-latest.json").read_text(encoding="utf-8"))

    def quote(self, symbol: str) -> dict[str, Any] | None:
        item = self._latest_by_symbol().get(symbol)
        if not item:
            return None
        return {
            "symbol": symbol,
            "name": item.get("name") or symbol,
            "price": item.get("price"),
            "close": item.get("price"),
            "previousClose": item.get("previous_close"),
            "change": item.get("change"),
            "changePercent": item.get("change_percent"),
            "open": item.get("open"),
            "high": item.get("high"),
            "low": item.get("low"),
            "volume": item.get("volume"),
            "turnover": item.get("turnover"),
            "turnoverRate": item.get("turnover_rate"),
            "marketState": "OPEN" if item.get("session") == "trading" else "CLOSED",
            "asOf": item.get("price_timestamp") or item.get("collected_at"),
            "quoteDate": str(item.get("collected_at") or "")[:10],
            "quality": item.get("quality"),
            "iopv": item.get("iopv"),
            "iopvDate": str(item.get("iopv_timestamp") or item.get("navDate") or "")[:10],
            "premiumPercent": item.get("computed_premium_percent", item.get("premiumPercent")),
            "vendorPremiumPercent": item.get("vendor_premium_percent"),
        }

    def fund_metric(self, symbol: str) -> dict[str, Any] | None:
        item = self._latest_by_symbol().get(symbol)
        if not item:
            return None
        if item.get("fundKind") or item.get("fundType"):
            result = dict(item)
            result["code"] = symbol
            result.setdefault("quality", {"status": "ok" if item.get("ok", True) else "degraded", "issues": []})
            return result
        return {
            "code": symbol,
            "name": item.get("name") or symbol,
            "price": item.get("price"),
            "previousClose": item.get("previous_close"),
            "changePercent": item.get("change_percent"),
            "premiumPercent": item.get("computed_premium_percent"),
            "vendorPremiumPercent": item.get("vendor_premium_percent"),
            "iopv": item.get("iopv"),
            "iopvDate": str(item.get("iopv_timestamp") or "")[:10],
            "volume": item.get("volume"),
            "turnover": item.get("turnover"),
            "turnoverRate": item.get("turnover_rate"),
            "suspended": bool(item.get("suspended")),
            "marketState": "OPEN" if item.get("session") == "trading" else "CLOSED",
            "asOf": item.get("collected_at"),
            "expiresAt": item.get("expires_at"),
            "quality": item.get("quality"),
        }

    def fund_metrics(self, symbols: list[str]) -> list[dict[str, Any]]:
        codes = list(dict.fromkeys(code for code in symbols if code.isdigit() and len(code) == 6))
        metrics = {code: self.fund_metric(code) for code in codes}
        if not codes:
            return []
        today = datetime.now(SHANGHAI).date()
        start = today - timedelta(days=45)

        def load_navs() -> dict[str, list[dict[str, Any]]]:
            payload = self.post_json(NAV_HISTORY_URL, {
                "codes": codes, "from": start.isoformat(), "to": today.isoformat(),
            }, self.timeout_sec)
            result: dict[str, list[dict[str, Any]]] = {}
            for entry in payload.get("items") or []:
                code = str(entry.get("code") or "")
                rows = ((entry.get("data") or {}).get("items")) or []
                valid = [
                    {"date": str(row.get("date") or "")[:10], "nav": _round4(row.get("nav"))}
                    for row in rows if _number(row.get("nav")) is not None and _number(row.get("nav")) > 0
                ]
                result[code] = valid
            return result

        cache_key = "latest-navs:" + ",".join(sorted(codes))
        missing_nav_codes = [code for code in codes if not (metrics.get(code) or {}).get("latestNav")]
        navs = self.cache.get_or_load(cache_key, 1800, load_navs) if missing_nav_codes else {}
        output = []
        for code in codes:
            metric = metrics.get(code)
            if not metric:
                continue
            rows = navs.get(code) or []
            if rows:
                latest = rows[-1]
                metric["latestNav"] = latest["nav"]
                metric["latestNavDate"] = latest["date"]
                if len(rows) > 1:
                    metric["previousNav"] = rows[-2]["nav"]
                    metric["previousNavDate"] = rows[-2]["date"]
            output.append(metric)
        return output

    def _raw_samples(self, symbol: str) -> list[dict[str, Any]]:
        samples = []
        for stored_sample in self.store.read_raw_samples(symbol, session="trading"):
            try:
                payload = dict(stored_sample)
                payload["_epoch"] = parse_iso(str(payload["collected_at"])).timestamp()
                samples.append(payload)
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(samples, key=lambda item: item["_epoch"])

    def intraday_klines(self, symbol: str, limit: int = 240) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sample in self._raw_samples(symbol):
            grouped[bucket_start_iso(str(sample["collected_at"]))].append(sample)
        candles = []
        for bucket, samples in sorted(grouped.items(), key=lambda item: parse_iso(item[0]).timestamp()):
            prices = [_number(item.get("price")) for item in samples]
            prices = [value for value in prices if value is not None and value > 0]
            if not prices:
                continue
            latest = samples[-1]
            premiums = [_number(item.get("computed_premium_percent")) for item in samples]
            premiums = [value for value in premiums if value is not None]
            iopv = _number(latest.get("iopv"))
            candle = {
                "t": int(parse_iso(bucket).timestamp()),
                "time": bucket,
                "date": bucket[:10],
                "o": _round4(prices[0]), "h": _round4(max(prices)),
                "l": _round4(min(prices)), "c": _round4(prices[-1]),
                "marketPrice": _round4(prices[-1]),
                "iopv": _round4(iopv), "nav": _round4(iopv),
                "premiumPercent": _round4(premiums[-1]) if premiums else None,
                "premiumOpen": _round4(premiums[0]) if premiums else None,
                "premiumHigh": _round4(max(premiums)) if premiums else None,
                "premiumLow": _round4(min(premiums)) if premiums else None,
                "premiumClose": _round4(premiums[-1]) if premiums else None,
                "sampleCount": len(samples),
                "quality": latest.get("quality"),
            }
            candles.append(candle)
        candles = candles[-max(1, min(limit, 3000)):]
        return {
            "market": "cn", "symbol": symbol, "interval": "5m",
            "generatedAt": _shanghai_iso(datetime.now(timezone.utc)),
            "source": f"market-collector-{self.store.backend_name}", "candles": candles,
        }

    def daily_price_klines(self, symbol: str, limit: int = 500) -> dict[str, Any]:
        limit = max(1, min(limit, 3000))
        secid = ("1." if symbol.startswith(("5", "6")) else "0.") + symbol
        params = urllib.parse.urlencode({
            "secid": secid, "klt": 101, "fqt": 1, "lmt": limit, "end": 20500101,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        })

        def load() -> dict[str, Any]:
            fallback_params = urllib.parse.urlencode({"tf": "1d", "limit": limit})
            try:
                fallback = self.fetch_json(f"{MARKETS_KLINE_URL}/{symbol}?{fallback_params}", self.timeout_sec)
                rows = []
                for item in fallback.get("candles") or []:
                    candle_date = str(item.get("date") or datetime.fromtimestamp(float(item.get("t") or 0), SHANGHAI).date().isoformat())[:10]
                    rows.append(",".join(str(value if value is not None else "") for value in [
                        candle_date, item.get("o"), item.get("c"), item.get("h"), item.get("l"),
                        item.get("v"), item.get("amount"), item.get("amplitudePercent"),
                        item.get("changePercent"), item.get("change"), item.get("turnoverRate"),
                    ]))
                raw = {"data": {"name": fallback.get("name") or symbol, "klines": rows}}
                source = "markets-worker"
            except Exception:
                raw = self.fetch_json(EASTMONEY_KLINE_URL + "?" + params, min(self.timeout_sec, 3.0))
                source = "eastmoney-push2his-fallback"
            data = raw.get("data") or {}
            candles = []
            for line in data.get("klines") or []:
                fields = str(line).split(",")
                if len(fields) < 11:
                    continue
                candle = {
                    "date": fields[0], "t": _date_epoch(fields[0]),
                    "o": _round4(fields[1]), "c": _round4(fields[2]),
                    "h": _round4(fields[3]), "l": _round4(fields[4]),
                    "v": _number(fields[5]), "amount": _number(fields[6]),
                    "amplitudePercent": _round4(fields[7]),
                    "changePercent": _round4(fields[8]), "change": _round4(fields[9]),
                    "turnoverRate": _round4(fields[10]),
                }
                candles.append(candle)
            return {
                "market": "cn", "symbol": symbol, "name": data.get("name") or symbol,
                "interval": "1d", "generatedAt": _shanghai_iso(datetime.now(timezone.utc)),
                "source": source, "candles": candles,
            }

        return self.cache.get_or_load(f"daily:{symbol}:{limit}", 300, load)

    def nav_history(self, symbol: str, days: int = 365) -> dict[str, Any]:
        days = max(1, min(days, 3650))
        to_date = datetime.now(SHANGHAI).date()
        from_date = to_date - timedelta(days=days)
        params = urllib.parse.urlencode({"code": symbol, "from": from_date.isoformat(), "to": to_date.isoformat()})

        def load() -> dict[str, Any]:
            payload = self.fetch_json(NAV_HISTORY_URL + "?" + params, self.timeout_sec)
            items = []
            for item in payload.get("items") or []:
                nav_date = str(item.get("date") or "")[:10]
                nav = _number(item.get("nav"))
                if nav_date and nav is not None and nav > 0:
                    items.append({"date": nav_date, "t": _date_epoch(nav_date), "nav": _round4(nav)})
            return {
                "symbol": symbol, "from": from_date.isoformat(), "to": to_date.isoformat(),
                "generatedAt": payload.get("generatedAt") or _shanghai_iso(datetime.now(timezone.utc)),
                "source": "holdings-nav-history", "items": items,
            }

        return self.cache.get_or_load(f"nav:{symbol}:{days}", 1800, load)

    def daily_combined(self, symbol: str, limit: int = 500) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=2) as executor:
            price_future = executor.submit(self.daily_price_klines, symbol, limit)
            nav_future = executor.submit(self.nav_history, symbol, max(365, int(limit * 1.7)))
            price_payload = price_future.result()
            nav_payload = nav_future.result()
        nav_items = nav_payload["items"]
        nav_dates = [item["date"] for item in nav_items]
        candles = []
        for raw in price_payload["candles"]:
            lookup_date = _previous_date(raw["date"])
            position = bisect.bisect_right(nav_dates, lookup_date) - 1
            nav_item = nav_items[position] if position >= 0 else None
            nav = _number(nav_item.get("nav")) if nav_item else None
            close = _number(raw.get("c"))
            premium = ((close / nav) - 1) * 100 if close is not None and nav is not None and nav > 0 else None
            candles.append({
                **raw,
                "marketPrice": close,
                "nav": _round4(nav),
                "iopv": _round4(nav),
                "navDate": nav_item.get("date") if nav_item else "",
                "premiumPercent": _round4(premium),
            })
        return {
            **price_payload,
            "candles": candles,
            "navCandles": [{"date": item["date"], "t": item["t"], "nav": item["nav"], "o": item["nav"], "h": item["nav"], "l": item["nav"], "c": item["nav"]} for item in nav_items],
            "navAlignment": "cross-border T-1",
        }

    def kline(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        return self.intraday_klines(symbol, limit) if interval == "5m" else self.daily_combined(symbol, limit)

    def premium_series(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        payload = self.kline(symbol, interval, limit)
        points = [{
            "time": item.get("time") or item.get("date"), "t": item.get("t"),
            "date": item.get("date"), "price": item.get("c"),
            "nav": item.get("nav"), "navDate": item.get("navDate") or item.get("date"),
            "premiumPercent": item.get("premiumPercent"),
        } for item in payload["candles"]]
        return {"symbol": symbol, "interval": interval, "generatedAt": payload["generatedAt"], "points": points}

    def home_overview(self) -> dict[str, Any]:
        latest = self._latest()
        records = latest.get("symbols") or []
        premiums = [_number(item.get("computed_premium_percent")) for item in records]
        premiums = [value for value in premiums if value is not None]
        changes = [_number(item.get("change_percent")) for item in records]
        changes = [value for value in changes if value is not None]
        price_ready = sum(_number(item.get("price")) is not None for item in records)
        premium_ready = len(premiums)
        total = len(SYMBOLS)
        market_state, session_label = _market_state()
        generated_at = latest.get("generated_at") or _shanghai_iso(datetime.now(timezone.utc))
        return {
            "schemaVersion": 1, "marketState": market_state, "sessionLabel": session_label,
            "generatedAt": generated_at, "priceAsOf": generated_at,
            "navAsOf": max((str(item.get("iopv_timestamp") or "") for item in records), default=""),
            "coverage": {
                "price": {"ready": price_ready, "total": total, "missing": total - price_ready, "stale": 0, "status": "ready" if price_ready == total else "partial"},
                "premium": {"ready": premium_ready, "total": total, "missing": total - premium_ready, "stale": 0, "status": "ready" if premium_ready == total else "partial"},
            },
            "breadth": {
                "riseCount": sum(value > 0 for value in changes),
                "fallCount": sum(value < 0 for value in changes),
                "flatCount": sum(value == 0 for value in changes),
                "premiumMedianPercent": _round4(statistics.median(premiums)) if premiums else None,
                "highPremiumCount": sum(value >= 5 for value in premiums),
            },
            "groups": [{key: value for key, value in group.items() if key != "codes"} for group in GROUPS],
            "anomalies": [], "source": "market-collector",
        }

    def home_series(self) -> dict[str, Any]:
        latest = self._latest_by_symbol()
        by_symbol = {symbol: self.intraday_klines(symbol, 240)["candles"] for symbol in SYMBOLS}
        group_for = {code: group["key"] for group in GROUPS[1:] for code in group["codes"]}
        price_series = []
        premium_series = []
        for symbol in SYMBOLS:
            rows = by_symbol[symbol]
            name = (latest.get(symbol) or {}).get("name") or symbol
            group_key = group_for.get(symbol, "all")
            price_series.append({"key": symbol, "code": symbol, "name": name, "groupKey": group_key, "points": [{"time": row["time"], "price": row["c"]} for row in rows]})
            premium_series.append({"key": symbol, "code": symbol, "name": name, "groupKey": group_key, "points": [{"time": row["time"], "price": row["c"], "nav": row["nav"], "premiumPercent": row["premiumPercent"], "navDate": row["date"]} for row in rows]})

        def aggregate_points(group: dict[str, Any], metric: str) -> list[dict[str, Any]]:
            values: dict[str, list[float]] = defaultdict(list)
            bases: dict[str, float] = {}
            for code in group["codes"]:
                for row in by_symbol[code]:
                    value = _number(row["c"] if metric == "price" else row["premiumPercent"])
                    if value is None:
                        continue
                    if metric == "price":
                        bases.setdefault(code, value)
                        value = value / bases[code] * 100
                    values[row["time"]].append(value)
            return [{"time": key, "value": _round4(statistics.mean(items) if metric == "price" else statistics.median(items))} for key, items in sorted(values.items())]

        price_aggregates = [{"key": f"price-equal-weight-{group['key']}", "role": "equal_weight", "label": group["label"] + "等权", "groupKey": group["key"], "normalized": True, "points": aggregate_points(group, "price")} for group in GROUPS]
        premium_aggregates = [{"key": f"premium-median-{group['key']}", "role": "median", "label": group["label"] + "中位数", "groupKey": group["key"], "points": aggregate_points(group, "premium")} for group in GROUPS]
        return {
            "schemaVersion": 1, "bucketMinutes": 5, "windowLabel": "今日 · 5 分钟", "defaultGroupKey": "all",
            "generatedAt": _shanghai_iso(datetime.now(timezone.utc)),
            "groups": [{key: value for key, value in group.items() if key != "codes"} for group in GROUPS],
            "modes": {
                "price": {"aggregate": {"series": price_aggregates}, "series": price_series},
                "premium": {"aggregate": {"series": premium_aggregates}, "series": premium_series},
            },
        }

    def market_summary(self, region: str) -> dict[str, Any] | None:
        from .indices import fetch_market_summary

        normalized = str(region or "CN").strip().upper()
        cache_key = "market-summary:" + normalized
        return self.cache.get_or_load(cache_key, 60, lambda: fetch_market_summary(normalized, self.timeout_sec))

    def fund_limit_overview(self) -> dict[str, Any]:
        import statistics as _stats

        rows = self.store.read_fund_reference_history("fund_limit", 30)
        if not rows:
            return {
                "schemaVersion": 1,
                "limitAsOf": None,
                "coverage": {"covered": 0, "total": 0, "review": 0},
                "currencyTotals": [],
                "trend": [],
                "events": [],
                "source": "market-collector",
            }
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_symbol.setdefault(str(row["symbol"]), []).append(row)
        # 排序每个 symbol 的快照
        for sym in by_symbol:
            by_symbol[sym].sort(key=lambda r: str(r["snapshot_date"]))
        latest_date = max(str(r["snapshot_date"]) for r in rows)
        latest_rows = [r for r in rows if str(r["snapshot_date"]) == latest_date]
        total_funds = len(by_symbol)

        def is_limited(p: dict[str, Any]) -> bool:
            s = str(p.get("buyStatus") or "").strip()
            return bool(s) and s != "open"

        def amount(p: dict[str, Any]) -> float | None:
            v = _number(p.get("maxPurchasePerDay"))
            return v if v is not None else None

        limited_latest = [r for r in latest_rows if is_limited(r["payload"])]
        review_count = sum(1 for r in latest_rows if str(r["payload"].get("buyStatus") or "") in ("limit_large", "suspend"))
        cny_amount = sum(amount(r["payload"]) or 0 for r in limited_latest)

        # 趋势
        by_date: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_date.setdefault(str(r["snapshot_date"]), []).append(r)
        trend = []
        for date_str in sorted(by_date):
            day = by_date[date_str]
            limited = [d for d in day if is_limited(d["payload"])]
            cny = sum(amount(d["payload"]) or 0 for d in limited)
            trend.append({"date": date_str, "cny": _round4(cny), "usd": None, "coveredCount": len(day)})

        # 事件：相邻快照 buyStatus / maxPurchasePerDay 变化
        events = []
        review_statuses = {"limit_large", "suspend"}
        for sym, snapshots in by_symbol.items():
            for i in range(1, len(snapshots)):
                before = snapshots[i - 1]["payload"]
                after = snapshots[i]["payload"]
                before_amt = amount(before)
                after_amt = amount(after)
                before_status = str(before.get("buyStatus") or "")
                after_status = str(after.get("buyStatus") or "")
                changed = before_status != after_status or (before_amt != after_amt) or (before_amt is None) != (after_amt is None)
                if changed:
                    events.append({
                        "id": ":".join([str(snapshots[i]["snapshot_date"]), sym, "scope_changed"]),
                        "type": "scope_changed",
                        "code": sym,
                        "name": after.get("fundName") or before.get("fundName") or sym,
                        "currency": "CNY",
                        "previousAmount": before_amt,
                        "currentAmount": after_amt,
                        "effectiveAt": str(snapshots[i]["snapshot_date"]),
                    })
        events.sort(key=lambda e: str(e.get("effectiveAt") or ""), reverse=True)
        return {
            "schemaVersion": 1,
            "limitAsOf": latest_date,
            "coverage": {"covered": len(latest_rows), "total": total_funds, "review": review_count},
            "currencyTotals": [{
                "currency": "CNY",
                "amount": _round4(cny_amount),
                "fundCount": len(latest_rows),
                "limitedCount": len(limited_latest),
            }],
            "trend": trend,
            "events": events[:100],
            "source": "market-collector",
        }

    def dataset_record(self, dataset: str, key: str) -> dict[str, Any] | None:
        payload: dict[str, Any] | None = None
        if dataset == "quote" and key.isdigit():
            payload = self.quote(key)
        elif dataset == "fund-metric" and key.isdigit():
            items = self.fund_metrics([key])
            payload = items[0] if items else None
        elif dataset == "kline" and ":" in key:
            symbol, interval = key.split(":", 1)
            payload = self.kline(symbol, interval, 500 if interval == "1d" else 240)
        elif dataset == "home-market-overview" and key == "global":
            payload = self.home_overview()
        elif dataset == "home-market-series" and key == "today:5m":
            payload = self.home_series()
        elif dataset == "market-summary" and key in ("CN", "US"):
            payload = self.market_summary(key)
        elif dataset == "fund-fee" and key.isdigit():
            refs = self.store.read_latest_fund_references("fund_fee", [key])
            payload = refs.get(key)
        elif dataset == "fund-limit-overview" and key == "global":
            payload = self.fund_limit_overview()
        if payload is None:
            return None
        updated_at = payload.get("generatedAt") or payload.get("asOf") or _shanghai_iso(datetime.now(timezone.utc))
        return {"_id": f"{dataset}:{key}", "dataset": dataset, "key": key, "status": "ready", "payload": payload, "version": int(time.time() * 1000), "updatedAt": updated_at}
