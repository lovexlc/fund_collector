from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from market_collector.http_server import (
    _is_web_api_route,
    _normalize_sec_financials,
    resolve_request,
)


class FakeMarketDataService:
    def quote(self, symbol: str):
        if symbol == "513100":
            return {
                "symbol": symbol,
                "price": 2.2,
                "asOf": "2026-08-11T10:00:00+08:00",
                "return1m": 2.3,
                "currentYearPercent": 11.78,
                "highPoint": {"price": 2.087, "highDate": "2026-06-02"},
            }
        return None

    def fund_metric(self, symbol: str):
        return {"code": symbol} if symbol == "513100" else None

    def fund_metrics(self, symbols: list[str]):
        return [
            {
                "code": code,
                "price": 2.2,
                "source": "local",
                "return1m": 2.3,
                "updatedAt": "2026-08-11T10:00:00+08:00",
            }
            for code in symbols if code == "513100"
        ]

    def home_overview(self):
        return {"marketState": "open"}

    def home_series(self):
        return {"modes": {"price": {}, "premium": {}}}

    def fund_limit_overview(self):
        return {"currencyTotals": [], "events": []}

    def kline(self, symbol: str, interval: str, limit: int):
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": [{"c": 2.2}] * min(limit, 2),
        }

    def fund_fees(self, symbols: list[str]):
        return [
            {"code": code, "ok": True, "data": {"managementFeeRate": 0.8}}
            if code == "513100" else
            {"code": code, "ok": False, "error": "fund fee unavailable"}
            for code in symbols
        ]

    def fund_limit(self, symbol: str):
        if symbol != "513100":
            return None
        return {"code": symbol, "buyStatus": "open", "maxPurchasePerDay": 1000.0}

    def indices(self, market: str):
        if market not in {"cn", "us"}:
            return None
        return {"market": market, "indexes": [{"symbol": "SPX", "price": 5000.0}]}

    def web_market_summary(self, region: str):
        if region not in {"CN", "US"}:
            return None
        return {"region": region, "items": [{"symbol": "SPX", "price": 5000.0}]}

    def xueqiu_fund_data(self, symbol: str):
        if symbol != "513100":
            return None
        return {"code": symbol, "results": {"quote_detail": {"ok": True}}}


class HttpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        (self.data_dir / "health.json").write_text(
            json.dumps({"healthy_symbols": 19, "degraded_symbols": 2}),
            encoding="utf-8",
        )
        (self.data_dir / "latest.json").write_text(
            json.dumps({"symbols": [{"symbol": "513100", "price": 2.24}]}),
            encoding="utf-8",
        )
        (self.data_dir / "otc-latest.json").write_text(
            json.dumps({"items": [{"code": "000834", "latestNav": 6.2}]}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_health_and_latest_routes(self) -> None:
        status, service = resolve_request("/", self.data_dir)
        self.assertEqual(status, 200)
        self.assertEqual(service["utc_offset"], "+08:00")

        status, health = resolve_request("/health", self.data_dir)
        self.assertEqual(status, 200)
        self.assertEqual(health["healthy_symbols"], 19)

        status, latest = resolve_request("/latest?source=test", self.data_dir)
        self.assertEqual(status, 200)
        self.assertEqual(len(latest["symbols"]), 1)

    def test_symbol_route_and_missing_symbol(self) -> None:
        status, record = resolve_request("/symbols/513100", self.data_dir)
        self.assertEqual(status, 200)
        self.assertEqual(record["price"], 2.24)

        status, error = resolve_request("/symbols/510300", self.data_dir)
        self.assertEqual(status, 404)
        self.assertEqual(error["error"], "symbol_not_found")

    def test_missing_snapshot_returns_service_unavailable(self) -> None:
        (self.data_dir / "health.json").unlink()
        status, payload = resolve_request("/health", self.data_dir)
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "snapshot_unavailable")

    def test_web_route_allowlist_excludes_mutating_admin_routes(self) -> None:
        self.assertTrue(_is_web_api_route("/news"))
        self.assertTrue(_is_web_api_route("/financials/AAPL"))
        self.assertTrue(_is_web_api_route("/fund-fee"))
        self.assertFalse(_is_web_api_route("/refresh"))
        self.assertFalse(_is_web_api_route("/ask"))
        self.assertFalse(_is_web_api_route("/kline-batch"))

    def test_quote_serves_local_record_with_summary_metrics(self) -> None:
        status, payload = resolve_request(
            "/quote/513100",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["price"], 2.2)
        self.assertEqual(payload["return1m"], 2.3)
        self.assertEqual(payload["currentYearPercent"], 11.78)
        self.assertEqual(payload["highPoint"]["price"], 2.087)
        self.assertEqual(payload["market"], "cn")
        self.assertEqual(payload["source"], "market-collector")

    def test_quote_missing_symbol_is_404_without_upstream(self) -> None:
        status, payload = resolve_request(
            "/quote/QQQ",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "symbol_not_found")

    def test_quotes_serve_local_only(self) -> None:
        status, payload = resolve_request(
            "/api/markets/quotes?symbols=513100,QQQ",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "market-collector")
        self.assertIn("513100", payload["quotes"])
        # 本地没有的代码直接缺席，不再回源 workers。
        self.assertNotIn("QQQ", payload["quotes"])
        self.assertEqual(payload["quotes"]["513100"]["price"], 2.2)
        self.assertEqual(payload["quotes"]["513100"]["return1m"], 2.3)

    def test_quotes_missing_symbols_is_404(self) -> None:
        status, payload = resolve_request(
            "/quotes?symbols=QQQ,SPY",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "symbols_not_found")

    def test_post_fund_metrics_serves_local_only(self) -> None:
        status, payload = resolve_request(
            "/fund-metrics",
            self.data_dir,
            FakeMarketDataService(),
            method="POST",
            body={"codes": ["513100", "000834"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["successCount"], 1)
        self.assertEqual(payload["failureCount"], 1)
        self.assertEqual(payload["items"][0]["code"], "513100")
        self.assertEqual(payload["items"][0]["return1m"], 2.3)

    def test_fund_fee_serves_local_snapshots(self) -> None:
        status, payload = resolve_request(
            "/api/market-collector/fund-fee?refresh=1",
            self.data_dir,
            FakeMarketDataService(),
            method="POST",
            body={"codes": ["513100", "000834"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "market-collector-local")
        self.assertEqual(payload["successCount"], 1)
        self.assertEqual(payload["failureCount"], 1)
        self.assertEqual(payload["items"][0]["code"], "513100")
        self.assertTrue(payload["items"][0]["ok"])
        self.assertFalse(payload["items"][1]["ok"])

    def test_fund_limit_serves_local_snapshot(self) -> None:
        status, payload = resolve_request(
            "/fund-limit?code=513100",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["buyStatus"], "open")
        self.assertEqual(payload["maxPurchasePerDay"], 1000.0)

        status, payload = resolve_request(
            "/fund-limit?code=000834",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "fund_limit_unavailable")

    def test_indices_and_market_summary_serve_local(self) -> None:
        status, payload = resolve_request(
            "/indices?market=cn",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["market"], "cn")

        status, payload = resolve_request(
            "/market-summary?region=US",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["region"], "US")

        status, payload = resolve_request(
            "/market-summary?region=EU",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "market_summary_unavailable")

    def test_sec_company_facts_are_normalized_for_financial_panel(self) -> None:
        payload = _normalize_sec_financials({
            "facts": {"us-gaap": {
                "Revenues": {"units": {"USD": [
                    {
                        "form": "10-K", "fp": "FY", "frame": "CY2025",
                        "end": "2025-09-27", "filed": "2025-10-31", "val": 100,
                    },
                    {
                        "form": "10-Q", "fp": "Q1", "frame": "CY2026Q1",
                        "end": "2025-12-27", "filed": "2026-01-30", "val": 30,
                    },
                ]}},
                "Assets": {"units": {"USD": [
                    {
                        "form": "10-K", "fp": "FY", "frame": "CY2025Q4I",
                        "end": "2025-09-27", "filed": "2025-10-31", "val": 500,
                    },
                    {
                        "form": "10-Q", "fp": "Q1", "frame": "CY2026Q1I",
                        "end": "2025-12-27", "filed": "2026-01-30", "val": 520,
                    },
                ]}},
            }},
        }, "AAPL")
        self.assertEqual(payload["source"], "sec-companyfacts")
        self.assertEqual(payload["statements"]["income"]["annual"][0]["fields"]["totalRevenue"], 100)
        self.assertEqual(payload["statements"]["income"]["quarterly"][0]["fields"]["totalRevenue"], 30)
        self.assertEqual(payload["statements"]["balance"]["quarterly"][0]["fields"]["totalAssets"], 520)

    def test_financials_uses_sec_source_directly(self) -> None:
        expected = {"symbol": "AAPL", "source": "sec-companyfacts", "statements": {}}

        status, payload = resolve_request(
            "/financials/AAPL?refresh=1",
            self.data_dir,
            FakeMarketDataService(),
            financials_request=lambda symbol, force: expected,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)

    def test_financials_sec_failure_returns_bad_gateway(self) -> None:
        def unavailable(*_args):
            raise OSError("SEC unavailable")

        status, payload = resolve_request(
            "/financials/AAPL",
            self.data_dir,
            FakeMarketDataService(),
            financials_request=unavailable,
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "financials_unavailable")

    def test_local_kline_served_without_upstream(self) -> None:
        status, payload = resolve_request(
            "/api/market-collector/kline/513100?tf=1d&limit=2",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["interval"], "1d")
        self.assertEqual(len(payload["candles"]), 2)

    def test_kline_missing_local_symbol_is_404(self) -> None:
        status, payload = resolve_request(
            "/kline/QQQ?tf=1d",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "kline_not_found")

    def test_unimplemented_web_route_returns_local_unavailable(self) -> None:
        # worker 专属功能（news/sectors/movers/...）明确本地不可用，绝不回源。
        status, payload = resolve_request(
            "/news?market=us",
            self.data_dir,
            FakeMarketDataService(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "local_data_unavailable")

    def test_home_aggregate_routes_are_local(self) -> None:
        service = FakeMarketDataService()
        for route in (
            "/api/market-collector/aggregates/home-market-overview",
            "/api/market-collector/aggregates/home-market-series",
            "/api/market-collector/aggregates/fund-limit-overview",
        ):
            status, payload = resolve_request(route, self.data_dir, service)
            self.assertEqual(status, 200)
            self.assertIsInstance(payload, dict)
        status, payload = resolve_request(
            "/api/market-collector/aggregates/home-market-collect", self.data_dir, service,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "market-collector-local")


if __name__ == "__main__":
    unittest.main()
