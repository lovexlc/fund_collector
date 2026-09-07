from __future__ import annotations

import unittest

from market_collector.danjuan import build_otc_item, fetch_nav_history, fetch_fund_detail, fund_kind


DETAIL = {
    "fd_code": "040046",
    "fd_name": "华安纳斯达克100指数A",
    "fd_full_name": "华安纳斯达克100交易型开放式指数证券投资基金联接基金(QDII)",
    "fd_type": "11",
    "type_desc": "QDII-股票",
    "fund_derived": {
        "end_date": "2026-09-03",
        "unit_nav": "8.1918",
        "nav_growth": "0.0901",
        "nav_grl1m": "1.9425812313",
        "nav_grl3m": "-4.742081027",
        "nav_grl6m": "16.1496143376",
        "nav_grlty": "11.8136030466",
        "nav_grl1y": "18.5362042021",
        "nav_grbase": "719.18",
    },
}


class DanjuanClientTest(unittest.TestCase):
    def test_fetch_nav_history_parses_and_sorts_ascending(self) -> None:
        def fetch_json(url, _timeout):
            self.assertIn("/djapi/fund/nav/history/513100", url)
            self.assertIn("size=3", url)
            return {
                "result_code": 0,
                "data": {"items": [
                    {"date": "2026-09-03", "nav": "1.9925", "percentage": "1.13"},
                    {"date": "2026-09-01", "nav": "1.9652", "percentage": "-1.32"},
                    {"date": "2026-09-02", "nav": "1.9702", "percentage": "0.25"},
                ]},
            }

        series = fetch_nav_history("513100", size=3, fetch_json=fetch_json)
        self.assertEqual([row["date"] for row in series], [
            "2026-09-01", "2026-09-02", "2026-09-03",
        ])
        self.assertEqual(series[-1]["nav"], 1.9925)

    def test_fetch_nav_history_invalid_code_returns_empty(self) -> None:
        self.assertEqual(fetch_nav_history("QQQ", fetch_json=lambda *_args: {}), [])

    def test_fetch_fund_detail_extracts_data_node(self) -> None:
        def fetch_json(_url, _timeout):
            return {"result_code": 0, "data": DETAIL}

        detail = fetch_fund_detail("040046", fetch_json=fetch_json)
        self.assertEqual(detail["fd_code"], "040046")
        self.assertEqual(detail["type_desc"], "QDII-股票")

    def test_fund_kind_detects_qdii(self) -> None:
        kind, text = fund_kind(DETAIL)
        self.assertEqual(kind, "qdii")
        self.assertEqual(text, "QDII-股票")

        kind, text = fund_kind({"type_desc": "混合型"})
        self.assertEqual(kind, "otc")
        self.assertEqual(text, "混合型")

        kind, text = fund_kind({"fd_type": "11"})
        self.assertEqual(kind, "qdii")

    def test_build_otc_item_shape(self) -> None:
        item = build_otc_item(DETAIL, "2026-09-07T19:30:00+08:00")
        self.assertEqual(item["code"], "040046")
        self.assertEqual(item["latestNav"], 8.1918)
        self.assertEqual(item["latestNavDate"], "2026-09-03")
        self.assertEqual(item["fundKind"], "qdii")
        self.assertEqual(item["fundType"], "QDII-股票")
        self.assertEqual(item["changePercent"], 0.0901)
        self.assertAlmostEqual(item["previousNav"], 8.1844, places=4)
        self.assertEqual(item["return1m"], 1.9426)
        self.assertEqual(item["returnBase"], 719.18)
        self.assertEqual(item["ytdReturn"], 11.8136)
        self.assertEqual(item["fallback"], "danjuan-direct")
        self.assertEqual(item["market"], "cn")
        self.assertIsNone(item["iopv"])

    def test_build_otc_item_rejects_code_without_nav(self) -> None:
        self.assertIsNone(build_otc_item({"fd_code": "04004"}, "now"))
        self.assertIsNone(build_otc_item({
            "fd_code": "040046", "fund_derived": {"unit_nav": None},
        }, "now"))


if __name__ == "__main__":
    unittest.main()
