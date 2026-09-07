from __future__ import annotations

import unittest
from unittest.mock import patch

from market_collector import netutil


class NetutilTest(unittest.TestCase):
    def test_direct_success_skips_proxy(self) -> None:
        with patch.object(netutil, "_open_direct", return_value=b"direct") as direct, \
                patch.object(netutil, "_open_via_proxy", return_value=b"proxy") as via_proxy:
            result = netutil.fetch_url("https://push2his.eastmoney.com/api", 5.0)
        self.assertEqual(result, b"direct")
        direct.assert_called_once()
        via_proxy.assert_not_called()

    def test_eastmoney_falls_back_to_egress_proxy(self) -> None:
        def fail(_request, _timeout):
            raise OSError("connection reset by WAF")

        with patch.object(netutil, "_open_direct", side_effect=fail), \
                patch.object(netutil, "_open_via_proxy", return_value=b"proxy-ok") as via_proxy:
            result = netutil.fetch_url("https://push2his.eastmoney.com/api/qt/stock/kline/get", 5.0)
        self.assertEqual(result, b"proxy-ok")
        via_proxy.assert_called_once()
        request, timeout = via_proxy.call_args[0]
        self.assertIn("push2his.eastmoney.com", request.full_url)
        self.assertEqual(timeout, 5.0)

    def test_non_fallback_host_raises_after_direct_failure(self) -> None:
        def fail(_request, _timeout):
            raise OSError("blocked")

        with patch.object(netutil, "_open_direct", side_effect=fail), \
                patch.object(netutil, "_open_via_proxy", return_value=b"never") as via_proxy:
            with self.assertRaises(OSError):
                netutil.fetch_url("https://qt.gtimg.cn/q=sh513100", 5.0)
        via_proxy.assert_not_called()

    def test_proxy_fallback_host_allowlist(self) -> None:
        self.assertTrue(netutil.proxy_fallback_enabled("https://push2his.eastmoney.com/x"))
        self.assertTrue(netutil.proxy_fallback_enabled("https://danjuanfunds.com/djapi/x"))
        self.assertFalse(netutil.proxy_fallback_enabled("https://qt.gtimg.cn/x"))
        self.assertFalse(netutil.proxy_fallback_enabled("https://api.freebacktrack.tech/x"))
        self.assertFalse(netutil.proxy_fallback_enabled("https://hq.sinajs.cn/x"))


if __name__ == "__main__":
    unittest.main()
