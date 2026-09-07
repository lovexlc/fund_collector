from __future__ import annotations

import unittest

from market_collector.otc import fetch_otc_metrics


def fake_detail(code: str):
    return {
        "fd_code": code,
        "fd_name": f"基金{code}",
        "fd_full_name": f"基金{code}全称",
        "fd_type": "11",
        "type_desc": "QDII-股票",
        "fund_derived": {
            "end_date": "2026-08-10",
            "unit_nav": "1.23",
            "nav_growth": "0.5",
            "nav_grl1m": "2.1",
            "nav_grl3m": "3.3",
            "nav_grl6m": "6.6",
            "nav_grl1y": "12.3",
            "nav_grlty": "9.9",
            "nav_grbase": "88.8",
        },
    }


class OtcCollectorTest(unittest.TestCase):
    def test_fetches_each_code_from_danjuan_directly(self) -> None:
        calls: list[str] = []

        def fetch_detail(code, _timeout):
            calls.append(code)
            return fake_detail(code)

        symbols = [f"{index:06d}" for index in range(5)]
        payload = fetch_otc_metrics(symbols, fetch_detail=fetch_detail)
        self.assertEqual(sorted(calls), sorted(symbols))
        self.assertEqual(payload["success_count"], 5)
        self.assertEqual(payload["failure_count"], 0)
        first = payload["items"][0]
        self.assertEqual(first["code"], symbols[0])
        self.assertEqual(first["latestNav"], 1.23)
        self.assertEqual(first["latestNavDate"], "2026-08-10")
        self.assertEqual(first["fundKind"], "qdii")
        self.assertEqual(first["fundType"], "QDII-股票")
        self.assertEqual(first["fallback"], "danjuan-direct")
        self.assertEqual(first["return1m"], 2.1)

    def test_failed_codes_are_reported_not_fatal(self) -> None:
        def fetch_detail(code, _timeout):
            if code == "000003":
                return None
            return fake_detail(code)

        payload = fetch_otc_metrics(
            ["000001", "000003"], fetch_detail=fetch_detail,
        )
        self.assertEqual(payload["success_count"], 1)
        self.assertEqual(payload["failure_count"], 1)
        self.assertEqual(len(payload["errors"]), 1)
        self.assertTrue(payload["errors"][0].startswith("000003"))


if __name__ == "__main__":
    unittest.main()
