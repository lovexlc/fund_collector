from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from market_collector.core import DEFAULT_CONFIG, MarketCollector, deep_update, due_daily_slot
from market_collector.fund_reference import fetch_fund_references, normalize_limit_payload


class FakeStore:
    backend_name = "sqlite"

    def __init__(self) -> None:
        self.reference_writes: list[dict] = []

    def initialize(self) -> None:
        pass

    def write_fund_reference_snapshots(self, records, retention_days) -> None:
        self.reference_writes.extend(records)


class FundReferenceTest(unittest.TestCase):
    def test_normalizes_direct_and_distributor_limits(self) -> None:
        payload = normalize_limit_payload({
            "code": "040046",
            "maxPurchasePerDay": 10,
            "channelLimits": {"direct": 100, "distributor": 10},
        })
        self.assertEqual(payload["channelLimits"], {"direct": 100.0, "distributor": 10.0})
        self.assertEqual(payload["maxPurchasePerDay"], 100.0)
        self.assertEqual(payload["limitChannel"], "app")
        self.assertEqual(payload["limitSchemaVersion"], 2)

    def test_fetches_fee_and_limit_from_direct_sources(self) -> None:
        def fetch_fees(code, _timeout):
            return {
                "code": code,
                "managementFeeRate": 0.8,
                "custodyFeeRate": 0.2,
                "salesServiceFeeRate": 0.0,
                "annualFeeRate": 1.0,
                "operationFeeRate": 0.8,
                "operationFees": [["管理费率", "0.80%（每年）"]],
                "purchaseRules": [],
                "redeemRules": [["申购状态", "开放申购", "赎回状态", "开放赎回", "定投状态", "支持"]],
                "purchaseStatusText": "开放申购",
                "source": "eastmoney-f10",
            }

        def fetch_info(code, _timeout):
            return {
                "code": code,
                "purchaseStatusText": "限大额",
                "purchaseStatusMark": "单日限额1000元",
                "minPurchase": 1.0,
                "maxPurchasePerDay": 1000.0,
                "fundName": "测试基金",
                "source": "eastmoney-fund-info",
            }

        payload = fetch_fund_references(
            ["513100"],
            now=datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc),
            fee_symbols=["513100"],
            limit_symbols=["513100"],
            fetch_fees=fetch_fees,
            fetch_info=fetch_info,
        )

        self.assertEqual(payload["snapshot_date"], "2026-08-12")
        self.assertEqual(payload["fee_success_count"], 1)
        self.assertEqual(payload["limit_success_count"], 1)
        self.assertEqual(len(payload["records"]), 2)
        self.assertEqual(payload["errors"], [])

        fee_record = payload["records"][0]
        self.assertEqual(fee_record["data_kind"], "fund_fee")
        self.assertEqual(fee_record["source"], "direct:fund-fee")
        self.assertEqual(fee_record["payload"]["managementFeeRate"], 0.8)
        self.assertEqual(fee_record["payload"]["fundName"], "测试基金")

        limit_record = payload["records"][1]
        self.assertEqual(limit_record["data_kind"], "fund_limit")
        self.assertEqual(limit_record["source"], "direct:fund-limit")
        self.assertEqual(limit_record["payload"]["buyStatus"], "limit_large")
        self.assertEqual(limit_record["payload"]["maxPurchasePerDay"], 1000.0)
        self.assertEqual(limit_record["payload"]["channelLimits"], {"all": 1000.0})
        self.assertEqual(limit_record["payload"]["limitSchemaVersion"], 2)

    def test_limit_record_missing_when_status_unavailable(self) -> None:
        payload = fetch_fund_references(
            ["000003"],
            now=datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc),
            limit_symbols=["000003"],
            fetch_fees=lambda code, _timeout: None,
            fetch_info=lambda code, _timeout: None,
        )
        self.assertEqual(payload["limit_success_count"], 0)
        self.assertEqual(payload["limit_failure_count"], 1)
        self.assertTrue(any("fund_limit:000003" in error for error in payload["errors"]))

    def test_daily_slot_is_due_after_time_and_only_once(self) -> None:
        completed = set()
        before = datetime(2026, 8, 12, 14, 29, tzinfo=timezone.utc)
        after = datetime(2026, 8, 12, 14, 31, tzinfo=timezone.utc)
        self.assertIsNone(due_daily_slot(before, "22:30", completed))
        self.assertEqual(due_daily_slot(after, "22:30", completed), "2026-08-12")
        completed.add("2026-08-12")
        self.assertIsNone(due_daily_slot(after, "22:30", completed))

    def test_scheduler_does_not_fetch_before_due_or_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = deep_update(DEFAULT_CONFIG, {
                "output_dir": temp_dir,
                "fund_reference_sync": {
                    "symbols": ["000001"],
                    "time": "22:30",
                },
                "publisher": {
                    "backend": "file",
                    "outbox_dir": str(Path(temp_dir) / "outbox"),
                },
            })
            store = FakeStore()
            collector = MarketCollector(config, store=store)
            result = {
                "records": [{
                    "data_kind": "fund_fee",
                    "symbol": "000001",
                    "snapshot_date": "2026-08-12",
                    "fetched_at": "2026-08-12T22:31:00+08:00",
                    "source": "direct:fund-fee",
                    "payload": {"code": "000001"},
                }],
                "requested_symbols": 1,
                "fee_success_count": 1,
                "limit_success_count": 0,
                "errors": [],
            }
            with patch("market_collector.core.fetch_fund_references", return_value=result) as fetch:
                collector.run_due_fund_reference_sync(
                    datetime(2026, 8, 12, 14, 29, tzinfo=timezone.utc)
                )
                self.assertEqual(fetch.call_count, 0)
                due = datetime(2026, 8, 12, 14, 31, tzinfo=timezone.utc)
                collector.run_due_fund_reference_sync(due)
                collector.run_due_fund_reference_sync(due)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(len(store.reference_writes), 1)


if __name__ == "__main__":
    unittest.main()
