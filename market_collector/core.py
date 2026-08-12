from __future__ import annotations

import copy
import json
import math
import time
from datetime import datetime, timedelta, time as day_time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .calendar_cn import is_trading_day
from .fund_reference import fetch_fund_references
from .otc import OTC_SYMBOLS, atomic_write_json, fetch_otc_metrics
from .publish import build_publisher
from .sources import fetch_eastmoney_references, fetch_tencent_quotes, isoformat_z, normalize_symbol
from .storage import MarketStore, build_store

SHANGHAI = ZoneInfo("Asia/Shanghai")

SYMBOLS = [
    "513870", "513390", "513300", "513110", "513100", "159941", "159696", "159660",
    "159659", "159632", "159513", "159509", "159501", "159577", "161128", "161130",
    "513500", "513650", "159612", "159655", "513850",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "symbols": SYMBOLS,
    "storage_backend": "sqlite",
    "database_path": "services/market-collector/data/market-collector.sqlite3",
    "output_dir": "services/market-collector/data/shadow",
    "request_timeout_sec": 10.0,
    "mismatch_tolerance_pp": 0.05,
    "raw_retention_hours": 168,
    "bucket_retention_days": 14,
    "schedules": {
        "trading": {"enabled": True, "interval_sec": 15, "ttl_sec": 90},
        "lunch": {"enabled": False, "interval_sec": 30, "ttl_sec": 900},
        "off_hours": {"enabled": False, "interval_sec": 30, "ttl_sec": 14400},
    },
    "otc": {
        "enabled": True,
        "times": ["19:30", "20:30", "21:30"],
        "symbols": OTC_SYMBOLS,
        "request_timeout_sec": 30,
    },
    "fund_reference_sync": {
        "enabled": True,
        "time": "22:30",
        "symbols": OTC_SYMBOLS,
        "worker_url": "https://api.freebacktrack.tech",
        "request_timeout_sec": 25,
        "concurrency": 4,
        "retention_days": 400,
    },
    "publisher": {
        "backend": "file",
        "worker_url": "https://api.freebacktrack.tech/api/markets/collector-ingest",
        "token_env": "MARKET_COLLECTOR_TOKEN",
        "outbox_dir": "services/market-collector/data/publish-outbox",
        "timeout_sec": 15,
    },
}


def deep_update(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_update(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(root: str, config_path: str | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if config_path:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        config = deep_update(config, payload)
    root_path = Path(root)
    workspace_root = root_path.parent.parent if len(root_path.parents) >= 2 else root_path.parent
    config["database_path"] = str((workspace_root / str(config["database_path"])).resolve()) if not Path(str(config["database_path"])).is_absolute() else str(config["database_path"])
    config["output_dir"] = str((workspace_root / str(config["output_dir"])).resolve()) if not Path(str(config["output_dir"])).is_absolute() else str(config["output_dir"])
    publisher = config.get("publisher") or {}
    outbox_dir = publisher.get("outbox_dir")
    if outbox_dir and not Path(str(outbox_dir)).is_absolute():
        publisher["outbox_dir"] = str((workspace_root / str(outbox_dir)).resolve())
    config["symbols"] = [normalize_symbol(symbol) for symbol in config.get("symbols", []) if normalize_symbol(symbol)]
    return config


def classify_session(now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI)
    if not is_trading_day(current):
        return "off_hours"
    clock = current.time()
    if day_time(9, 30) <= clock < day_time(11, 30):
        return "trading"
    if day_time(11, 30) <= clock < day_time(13, 0):
        return "lunch"
    if day_time(13, 0) <= clock < day_time(15, 31):
        return "trading"
    return "off_hours"


def due_otc_slot(now: datetime, times: list[str], completed: set[str]) -> str | None:
    current = now.astimezone(SHANGHAI)
    if not is_trading_day(current):
        return None
    clock = current.strftime("%H:%M")
    if clock not in times:
        return None
    key = current.date().isoformat() + "T" + clock
    return None if key in completed else key


def due_daily_slot(now: datetime, scheduled_time: str, completed: set[str]) -> str | None:
    current = now.astimezone(SHANGHAI)
    try:
        scheduled_clock = datetime.strptime(str(scheduled_time), "%H:%M").time()
    except ValueError:
        return None
    key = current.date().isoformat()
    if current.time().replace(tzinfo=None) < scheduled_clock or key in completed:
        return None
    return key


def compute_premium(price: float | None, iopv: float | None) -> float | None:
    if price is None or iopv is None or iopv <= 0:
        return None
    return round(((price - iopv) / iopv) * 100, 4)


def symbol_category(symbol: str) -> str:
    return "lof" if symbol in {"161128", "161130"} else "cross_border_etf"


def build_symbol_record(
    symbol: str,
    price_row: dict[str, Any] | None,
    iopv_row: dict[str, Any] | None,
    collected_at: str,
    session: str,
    ttl_sec: int,
    mismatch_tolerance_pp: float,
) -> dict[str, Any]:
    price = price_row.get("price") if price_row else None
    iopv = iopv_row.get("iopv") if iopv_row else None
    vendor_premium = iopv_row.get("vendor_premium_percent") if iopv_row else None
    computed_premium = compute_premium(price, iopv)
    mismatch_pp = round(abs(computed_premium - vendor_premium), 4) if computed_premium is not None and vendor_premium is not None else None
    quality_issues: list[str] = []
    if price is None:
        quality_issues.append("missing_price")
    if iopv is None:
        quality_issues.append("missing_iopv")
    if vendor_premium is None:
        quality_issues.append("missing_vendor_premium")
    if mismatch_pp is not None and mismatch_pp > mismatch_tolerance_pp:
        quality_issues.append("premium_mismatch")
    quality_status = "ok" if not quality_issues else ("degraded" if len(quality_issues) < 3 else "missing")
    expires_at = isoformat_z(datetime.fromisoformat(collected_at.replace("Z", "+00:00")) + timedelta_seconds(ttl_sec))
    return {
        "symbol": symbol,
        "name": (price_row or iopv_row or {}).get("name") or symbol,
        "category": symbol_category(symbol),
        "session": session,
        "collected_at": collected_at,
        "price_timestamp": (price_row or {}).get("source_as_of") or (price_row or {}).get("received_at"),
        "iopv_timestamp": (iopv_row or {}).get("source_as_of") or (iopv_row or {}).get("received_at"),
        "price_received_at": (price_row or {}).get("received_at"),
        "iopv_received_at": (iopv_row or {}).get("received_at"),
        "price": price,
        "previous_close": (price_row or {}).get("previous_close"),
        "change": (price_row or {}).get("change"),
        "change_percent": (price_row or {}).get("change_percent"),
        "open": (price_row or {}).get("open"),
        "high": (price_row or {}).get("high"),
        "low": (price_row or {}).get("low"),
        "volume": (price_row or {}).get("volume"),
        "turnover": (price_row or {}).get("turnover"),
        "turnover_rate": (price_row or {}).get("turnover_rate"),
        "iopv": iopv,
        "computed_premium_percent": computed_premium,
        "vendor_premium_percent": vendor_premium,
        "vendor_discount_percent_raw": (iopv_row or {}).get("vendor_discount_percent_raw"),
        "mismatch_pp": mismatch_pp,
        "expires_at": expires_at,
        "ttl_sec": ttl_sec,
        "sources": {
            "price": (price_row or {}).get("source"),
            "iopv": (iopv_row or {}).get("source"),
        },
        "quality": {
            "status": quality_status,
            "issues": quality_issues,
        },
        "debug": {
            "eastmoney_page": (iopv_row or {}).get("page"),
        },
    }


def timedelta_seconds(seconds: int) -> Any:
    from datetime import timedelta
    return timedelta(seconds=seconds)


class MarketCollector:
    def __init__(self, config: dict[str, Any], store: MarketStore | None = None) -> None:
        self.config = config
        self.store = store if store is not None else build_store(config)
        self.publisher = build_publisher(config)
        self._otc_completed_slots: set[str] = set()
        self._fund_reference_completed_dates: set[str] = set()
        self._daily_kline_completed_dates: set[str] = set()
        self._data_service: MarketDataService | None = None
        from .fund_store import build_fund_store, FundStore
        self.fund_store: FundStore | None = build_fund_store(config)
        if self.fund_store is not None:
            self.fund_store.initialize()
        self.store.initialize()

    def collect_otc_once(self, slot: str) -> dict[str, Any]:
        otc_config = self.config.get("otc") or {}
        payload = fetch_otc_metrics(
            symbols=list(otc_config.get("symbols") or OTC_SYMBOLS),
            timeout_sec=float(otc_config.get("request_timeout_sec") or 30),
        )
        payload["scheduled_slot"] = slot
        atomic_write_json(Path(str(self.config["output_dir"])) / "otc-latest.json", payload)
        return payload

    def run_due_otc(self, now: datetime) -> dict[str, Any] | None:
        otc_config = self.config.get("otc") or {}
        if otc_config.get("enabled", True) is False:
            return None
        slot = due_otc_slot(now, list(otc_config.get("times") or []), self._otc_completed_slots)
        if slot is None:
            return None
        payload = self.collect_otc_once(slot)
        self._otc_completed_slots.add(slot)
        current_date = now.astimezone(SHANGHAI).date().isoformat()
        self._otc_completed_slots = {key for key in self._otc_completed_slots if key.startswith(current_date)}
        return payload

    def collect_fund_references_once(self, now: datetime | None = None) -> dict[str, Any]:
        sync_config = self.config.get("fund_reference_sync") or {}
        otc_symbols = list(sync_config.get("symbols") or OTC_SYMBOLS)
        etf_symbols = list(self.config.get("symbols") or SYMBOLS)
        # fee 同时抓场内 + 场外：场内 ETF 取管理费/托管费/年费，场外取 redeemRules 卖出费率阶梯；
        # limit 只抓场外：场内 ETF 无限购概念。
        fee_symbols = list(dict.fromkeys(otc_symbols + etf_symbols))
        payload = fetch_fund_references(
            symbols=otc_symbols,
            worker_url=str(sync_config.get("worker_url") or "https://api.freebacktrack.tech"),
            timeout_sec=float(sync_config.get("request_timeout_sec") or 25),
            concurrency=int(sync_config.get("concurrency") or 4),
            now=now,
            fee_symbols=fee_symbols,
            limit_symbols=otc_symbols,
        )
        self.store.write_fund_reference_snapshots(
            list(payload.get("records") or []),
            retention_days=int(sync_config.get("retention_days") or 400),
        )
        output = dict(payload)
        output.pop("records", None)
        atomic_write_json(
            Path(str(self.config["output_dir"])) / "fund-reference-sync-latest.json",
            output,
        )
        return payload

    def run_due_fund_reference_sync(self, now: datetime) -> dict[str, Any] | None:
        sync_config = self.config.get("fund_reference_sync") or {}
        if sync_config.get("enabled", True) is False:
            return None
        slot = due_daily_slot(
            now,
            str(sync_config.get("time") or "22:30"),
            self._fund_reference_completed_dates,
        )
        if slot is None:
            return None
        payload = self.collect_fund_references_once(now)
        self._fund_reference_completed_dates.add(slot)
        self._fund_reference_completed_dates = {slot}
        print(
            "[fund-reference-sync] "
            f"date={slot} fee={payload.get('fee_success_count')}/{payload.get('requested_symbols')} "
            f"limit={payload.get('limit_success_count')}/{payload.get('requested_symbols')} "
            f"errors={len(payload.get('errors') or [])}"
        )
        return payload

    def collect_once(self) -> tuple[dict[str, Any], dict[str, Any]]:
        collected_at = isoformat_z(datetime.now(timezone.utc))
        session = classify_session(datetime.now(timezone.utc))
        ttl_sec = int((((self.config.get("schedules") or {}).get(session)) or {}).get("ttl_sec") or 300)
        symbols = list(self.config["symbols"])
        timeout_sec = float(self.config["request_timeout_sec"])
        source_errors: dict[str, str] = {}
        try:
            price_map = fetch_tencent_quotes(symbols, timeout_sec)
        except Exception as exc:
            price_map = {}
            source_errors["tencent_batch"] = str(exc)
        try:
            iopv_map, eastmoney_meta = fetch_eastmoney_references(symbols, timeout_sec)
        except Exception as exc:
            iopv_map, eastmoney_meta = {}, {"page_size": 100, "pages_visited": 0, "missing_symbols": symbols}
            source_errors["eastmoney_push2delay"] = str(exc)
        records = [
            build_symbol_record(
                symbol=symbol,
                price_row=price_map.get(symbol),
                iopv_row=iopv_map.get(symbol),
                collected_at=collected_at,
                session=session,
                ttl_sec=ttl_sec,
                mismatch_tolerance_pp=float(self.config["mismatch_tolerance_pp"]),
            )
            for symbol in symbols
        ]
        self.store.write_cycle(
            records,
            raw_retention_hours=int(self.config["raw_retention_hours"]),
            bucket_retention_days=int(self.config["bucket_retention_days"]),
        )
        healthy = sum(1 for item in records if item["quality"]["status"] == "ok")
        degraded = len(records) - healthy
        latest_payload = {
            "kind": "market-collector-shadow-latest",
            "generated_at": collected_at,
            "session": session,
            "symbols": records,
        }
        health_payload = {
            "kind": "market-collector-shadow-health",
            "generated_at": collected_at,
            "session": session,
            "schedule": (self.config.get("schedules") or {}).get(session),
            "healthy_symbols": healthy,
            "degraded_symbols": degraded,
            "source_errors": source_errors,
            "eastmoney_pagination": eastmoney_meta,
        }
        self.publisher.publish(latest_payload, health_payload)
        return latest_payload, health_payload

    def _ensure_data_service(self) -> MarketDataService:
        from .aggregates import MarketDataService
        if self._data_service is None:
            self._data_service = MarketDataService(self.store, self.config["output_dir"])
        return self._data_service

    # ── 4 张产品表的写入 ──
    def _publish_quotes(self) -> int:
        """盘中 5 分钟：ETF（从 raw_samples 最新）+ OTC（从 otc-latest）写 fund_quote。"""
        if self.fund_store is None:
            return 0
        data_service = self._ensure_data_service()
        rows: list[dict[str, Any]] = []
        # 场内 ETF：用 fund_metric 取数（含 iopv / premiumPercent），quote() 缺这俩字段
        for symbol in self.config["symbols"]:
            fm = data_service.fund_metric(symbol)
            if fm and fm.get("price"):
                rows.append({
                    "code": symbol,
                    "name": fm.get("name") or symbol,
                    "price": fm.get("price"),
                    "latestNav": None,
                    "latestNavDate": None,
                    "previousClose": fm.get("previousClose"),
                    "changePercent": fm.get("changePercent"),
                    "premiumPercent": fm.get("premiumPercent"),
                    "iopv": fm.get("iopv"),
                    "volume": fm.get("volume"),
                    "turnover": fm.get("turnover"),
                    "marketState": fm.get("marketState"),
                    "asOf": fm.get("asOf"),
                    "session": "exchange",
                })
        # 场外 OTC（净值作为最新价）—— 字段集与场内对齐，缺失填 None，
        # 避免 upsert_quotes 批量 SQL 因参数数量不一致而错位。
        for item in (data_service.otc_latest().get("items") or []):
            code = str(item.get("code") or item.get("symbol") or "")
            if not code:
                continue
            rows.append({
                "code": code,
                "name": item.get("name") or code,
                "price": item.get("latestNav") or item.get("price"),
                "latestNav": item.get("latestNav"),
                "latestNavDate": str(item.get("latestNavDate") or ""),
                "previousClose": item.get("previousClose") or item.get("previousNav"),
                "changePercent": item.get("changePercent"),
                "premiumPercent": None,
                "iopv": None,
                "volume": item.get("volume"),
                "turnover": item.get("turnover"),
                "marketState": item.get("marketState") or "CLOSED",
                "asOf": item.get("asOf") or item.get("updatedAt"),
                "session": "otc",
            })
        return self.fund_store.upsert_quotes(rows)

    def _publish_details(self) -> int:
        """每日：从 fund_reference 同步限额 + 费率写 fund_detail。"""
        if self.fund_store is None:
            return 0
        etf_symbols = list(self.config.get("symbols") or SYMBOLS)
        otc_symbols = list((self.config.get("fund_reference_sync") or {}).get("symbols") or OTC_SYMBOLS)
        all_codes = list(dict.fromkeys(etf_symbols + otc_symbols))
        rows: list[dict[str, Any]] = []
        for code in all_codes:
            fee = self.store.read_latest_fund_references("fund_fee", [code]).get(code) or {}
            lim = self.store.read_latest_fund_references("fund_limit", [code]).get(code) or {}
            if not fee and not lim:
                continue
            is_etf = code in etf_symbols
            rows.append({
                "code": code,
                "name": fee.get("fundName") or lim.get("fundName") or code,
                "full_name": fee.get("fullName"),
                "fund_type": ("exchange" if is_etf else "otc"),
                "exchange": ("exchange" if is_etf else "otc"),
                "region": "cn",
                "buy_status": lim.get("buyStatus"),
                "buy_status_text": lim.get("buyStatusText"),
                "max_purchase_per_day": lim.get("maxPurchasePerDay"),
                "min_purchase": lim.get("minPurchase"),
                "confirm_days": lim.get("confirmDays"),
                "management_fee_rate": fee.get("managementFeeRate"),
                "custody_fee_rate": fee.get("custodyFeeRate"),
                "annual_fee_rate": fee.get("annualFeeRate"),
                "sales_service_fee_rate": fee.get("salesServiceFeeRate"),
                "redeem_fee_rate": fee.get("redeemFeeRate"),
                "redeem_rules": fee.get("redeemRules"),
                "operation_fees": fee.get("operationFees"),
                "fund_size": fee.get("fundSize"),
            })
        return self.fund_store.upsert_details(rows)

    def _fetch_nav_history_rows(self, codes: list[str]) -> list[dict[str, Any]]:
        """拉历史净值 K 线（增量，只取最近未入库的）。"""
        import urllib.parse
        import urllib.request
        import json as _json
        from .sources import isoformat_z as _iso
        timeout = float((self.config.get("fund_reference_sync") or {}).get("request_timeout_sec") or 25)
        today = datetime.now(SHANGHAI).date().isoformat()
        out: list[dict[str, Any]] = []
        for i in range(0, len(codes), 5):
            batch = codes[i:i + 5]
            body = _json.dumps({"codes": batch, "from": "2024-01-01", "to": today}).encode()
            req = urllib.request.Request(
                "https://api.freebacktrack.tech/api/holdings/nav-history",
                data=body, method="POST",
                headers={"content-type": "application/json", "user-agent": "curl/8.4.0"},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    payload = _json.loads(r.read().decode("utf-8", "replace"))
            except Exception as exc:
                print(f"[fund-store] nav-history batch fail: {exc}", flush=True)
                continue
            for entry in (payload.get("items") or []):
                code = str(entry.get("code") or "")
                rows = ((entry.get("data") or {}).get("items")) or []
                for row in rows:
                    nav = row.get("nav")
                    try:
                        nav_f = float(nav)
                    except (TypeError, ValueError):
                        continue
                    if not (nav_f > 0):
                        continue
                    out.append({"code": code, "date": str(row.get("date") or "")[:10], "nav": nav_f, "source": "holdings-nav-history"})
        return out

    def _publish_history_and_summary(self) -> tuple[int, int]:
        """每日：拉净值历史增量写 fund_history，再预算 fund_summary。"""
        if self.fund_store is None:
            return 0, 0
        etf_symbols = list(self.config.get("symbols") or SYMBOLS)
        otc_symbols = list((self.config.get("fund_reference_sync") or {}).get("symbols") or OTC_SYMBOLS)
        all_codes = list(dict.fromkeys(etf_symbols + otc_symbols))
        hist_rows = self._fetch_nav_history_rows(all_codes)
        hist_n = self.fund_store.upsert_history(hist_rows)
        # 预算 summary
        import datetime as _dt
        RETURN_WINDOWS = [("return_1w", 7), ("return_1m", 31), ("return_3m", 93), ("return_6m", 186), ("return_1y", 365)]
        today_str = datetime.now(SHANGHAI).date().isoformat()
        def _num(v):
            if v in (None, ""):
                return None
            try:
                f = float(v); return f if f == f else None
            except (TypeError, ValueError):
                return None
        def pct(cur, base):
            a, b = _num(cur), _num(base)
            if a is None or b is None or b <= 0:
                return None
            return round((a - b) / b * 10000) / 100
        def hist_pct(closes, current):
            current = _num(current); cs = [x for x in (_num(c) for c in closes) if x is not None]
            if current is None or not cs:
                return None
            return round(sum(1 for x in cs if x <= current) / len(cs) * 10000) / 100
        def dd_pct(closes, current):
            current = _num(current); cs = [x for x in (_num(c) for c in closes) if x is not None and x > 0]
            if current is None or current <= 0 or len(cs) < 2:
                return None
            rm = -float("inf"); dds = []
            for cl in cs:
                if cl > rm:
                    rm = cl
                dds.append((cl / rm - 1) * 100)
            cur_dd = (current / rm - 1) * 100
            if cur_dd != cur_dd:
                return None
            return round(sum(1 for d in dds if d >= cur_dd) / len(dds) * 10000) / 100
        summary_rows: list[dict[str, Any]] = []
        for code in all_codes:
            pts = self.fund_store.read_history(code)
            if not pts:
                continue
            pts.sort(key=lambda x: x[0])
            dates = [d for (d, _n) in pts]; navs = [n for (_d, n) in pts]
            last_nav = navs[-1] if navs else None
            if not last_nav:
                continue
            last_t = int(_dt.datetime.fromisoformat(dates[-1] + "T00:00:00+08:00").timestamp())
            def cab(t):
                sel = None
                for d, n in pts:
                    tt = int(_dt.datetime.fromisoformat(d + "T00:00:00+08:00").timestamp())
                    if tt <= t:
                        sel = n
                    else:
                        break
                return sel
            r = {k: pct(last_nav, cab(last_t - days * 86400)) for k, days in RETURN_WINDOWS}
            year = dates[-1][:4]
            ytd_base = None
            for d, n in pts:
                if d >= year + "-01-01":
                    ytd_base = n; break
            r["ytd_return"] = pct(last_nav, ytd_base)
            r["return_base"] = pct(last_nav, navs[0])
            r["historical_percentile"] = hist_pct(navs, last_nav)
            r["drawdown_percentile"] = dd_pct(navs, last_nav)
            cutoff = last_t - 365 * 86400
            high = None; high_date = ""
            for d, n in pts:
                tt = int(_dt.datetime.fromisoformat(d + "T00:00:00+08:00").timestamp())
                if tt < cutoff:
                    continue
                if high is None or n > high:
                    high = n; high_date = d
            chd = round((last_nav / high - 1) * 10000) / 100 if high and high > 0 else None
            summary_rows.append({
                "code": code, "date": today_str, "latest_nav": last_nav,
                **r, "high_drawdown": chd, "close_high_drawdown": chd,
                "high_point": high, "high_point_date": high_date,
                "close_high_point": high, "close_high_point_date": high_date,
            })
        sum_n = self.fund_store.upsert_summaries(summary_rows)
        return hist_n, sum_n

    def run_forever(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            otc_payload = self.run_due_otc(now)
            if otc_payload is not None:
                # OTC 净值刷新后：更新 fund_quote（场外净值）+ fund_summary（收益/水位）
                try:
                    n = self._publish_quotes()
                    _hn, sn = self._publish_history_and_summary()
                    print(f"[fund-store] otc refresh: quote={n} summary={sn}", flush=True)
                except Exception as exc:
                    print(f"[fund-store] otc refresh failed: {exc}", flush=True)
            if self.run_due_fund_reference_sync(now) is not None:
                # 晚间费率/限额同步后：更新 fund_detail + fund_history/summary
                try:
                    dn = self._publish_details()
                    _hn, sn = self._publish_history_and_summary()
                    print(f"[fund-store] daily: detail={dn} summary={sn}", flush=True)
                except Exception as exc:
                    print(f"[fund-store] daily failed: {exc}", flush=True)
            session = classify_session(now)
            now_sh = now.astimezone(SHANGHAI)
            # 5 分钟边界：盘中实时行情写 fund_quote
            if now_sh.minute % 5 == 0:
                try:
                    n = self._publish_quotes()
                    if n:
                        print(f"[fund-store] intraday quote={n} at {isoformat_z(now)}", flush=True)
                except Exception as exc:
                    print(f"[fund-store] intraday failed: {exc}", flush=True)
            schedule = ((self.config.get("schedules") or {}).get(session)) or {}
            interval_sec = max(1, int(schedule.get("interval_sec") or 60))
            enabled = schedule.get("enabled", True) is not False
            if enabled:
                self.collect_once()
            time.sleep(interval_sec)
