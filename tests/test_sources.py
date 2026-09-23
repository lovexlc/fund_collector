from __future__ import annotations

import json
import unittest

from market_collector.sources import fetch_eastmoney_references, parse_eastmoney_list_payload, parse_tencent_quote_text


class SourceParserTest(unittest.TestCase):
    def test_parse_tencent_batch_quote(self) -> None:
        payload = 'v_sh513100="1~纳指ETF国泰~513100~2.236~2.261~2.250~571425~12769~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260811145501~-0.025~-1.11~2.260~2.220";'
        result = parse_tencent_quote_text(payload, "2026-08-11T10:00:00+08:00")

        self.assertEqual(result["513100"]["symbol"], "513100")
        self.assertEqual(result["513100"]["price"], 2.236)
        self.assertEqual(result["513100"]["open"], 2.25)
        self.assertEqual(result["513100"]["volume"], 571425)
        self.assertEqual(result["513100"]["source"], "tencent_batch")
        self.assertEqual(result["513100"]["source_as_of"], "2026-08-11T14:55:01+08:00")

    def test_parse_eastmoney_sign_corrects_vendor_premium(self) -> None:
        payload = {
            "data": {
                "diff": [
                    {"f12": "513100", "f14": "纳指ETF国泰", "f2": 2.237, "f124": 1723447200, "f402": -11.58, "f441": 2.0048}
                ]
            }
        }
        result = parse_eastmoney_list_payload(payload, "2026-08-11T10:00:01+08:00", page=24)

        self.assertEqual(result["513100"]["iopv"], 2.0048)
        self.assertEqual(result["513100"]["vendor_discount_percent_raw"], -11.58)
        self.assertEqual(result["513100"]["vendor_premium_percent"], 11.58)
        self.assertEqual(result["513100"]["page"], 24)
        self.assertEqual(result["513100"]["source_as_of"], "2024-08-12T15:20:00+08:00")

    def test_clist_failure_falls_back_to_ulist(self) -> None:
        ulist_payload = json.dumps({
            "data": {"diff": [{"f12": "513100", "f14": "纳指ETF国泰", "f2": 2.3, "f402": -12.92, "f441": 2.051}]}
        }).encode("utf-8")

        def fake_fetch(url: str, _timeout: float) -> bytes:
            if "/clist/" in url:
                raise OSError("Remote end closed connection without response")
            return ulist_payload

        found, meta = fetch_eastmoney_references(["513100"], 5.0, fetch_bytes=fake_fetch)
        self.assertIn("513100", found)
        self.assertEqual(found["513100"]["iopv"], 2.051)
        self.assertEqual(found["513100"]["vendor_premium_percent"], 12.92)
        self.assertEqual(meta["missing_symbols"], [])

    def test_transport_failure_raises_instead_of_silent_empty(self) -> None:
        def fake_fetch(_url: str, _timeout: float) -> bytes:
            raise OSError("Remote end closed connection without response")

        with self.assertRaises(OSError):
            fetch_eastmoney_references(["513100"], 5.0, fetch_bytes=fake_fetch)


if __name__ == "__main__":
    unittest.main()
