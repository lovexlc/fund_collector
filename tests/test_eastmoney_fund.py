from __future__ import annotations

import unittest

from market_collector.eastmoney_fund import (
    build_limit_payload,
    fetch_f10_fees,
    fetch_fund_info,
)


F10_HTML = """
<html><body>
<div id="nav">申购状态 分红 公告 私募</div>
<table>
<tr><td>管理费率</td><td><span>0.80%</span>（每年）</td>
    <td>托管费率</td><td><span>0.20%</span>（每年）</td>
    <td>销售服务费率</td><td><span>0.00%</span>（每年）</td></tr>
</table>
<table>
<tr><td>申购状态</td><td>限大额</td></tr>
<tr><td>赎回状态</td><td>开放赎回</td></tr>
<tr><td>定投状态</td><td>不支持</td></tr>
</table>
<table>
<tr><td>适用期限</td><td>赎回费率</td></tr>
<tr><td>小于7天</td><td>1.50%</td></tr>
<tr><td>大于等于7天，小于1年</td><td>0.50%</td></tr>
<tr><td>大于等于1年</td><td>0.00%</td></tr>
</table>
</body></html>
"""

FUND_INFO_PAYLOAD = {
    "Datas": {
        "SGZT": "限大额",
        "SGZTMARK": "单日单账户限额1000元",
        "MINSG": "1",
        "MAXSG": "1000",
        "SHORTNAME": "纳指ETF联接",
        "FEGM": "9465110600",
        "ENDNAV": "19468268721.53",
        "FEGMRQ": "2026-06-30 00:00:00",
    },
    "ErrCode": 0,
}


class EastmoneyFundTest(unittest.TestCase):
    def test_fetch_f10_fees_parses_operation_rates_and_status(self) -> None:
        fees = fetch_f10_fees("000055", fetch_text=lambda _url, _timeout: F10_HTML)
        self.assertEqual(fees["managementFeeRate"], 0.8)
        self.assertEqual(fees["custodyFeeRate"], 0.2)
        self.assertEqual(fees["salesServiceFeeRate"], 0.0)
        self.assertEqual(fees["annualFeeRate"], 1.0)
        self.assertEqual(fees["purchaseStatusText"], "限大额")
        self.assertEqual(fees["redeemStatusText"], "开放赎回")
        # 每项费率一行：前端 combineRuleRates 每行只取一个百分数再求和
        self.assertEqual(fees["operationFees"], [
            ["管理费率", "0.80%（每年）"],
            ["托管费率", "0.20%（每年）"],
            ["销售服务费率", "0.00%（每年）"],
        ])
        # 申赎状态行在前，赎回费率阶梯行随后（表头被过滤）
        self.assertEqual(fees["redeemRules"], [
            ["申购状态", "限大额", "赎回状态", "开放赎回", "定投状态", "不支持"],
            ["小于7天", "1.50%"],
            ["大于等于7天，小于1年", "0.50%"],
            ["大于等于1年", "0.00%"],
        ])
        self.assertEqual(fees["source"], "eastmoney-f10")

    def test_fetch_f10_fees_returns_none_when_no_rates(self) -> None:
        self.assertIsNone(fetch_f10_fees(
            "000055", fetch_text=lambda _url, _timeout: "<html>页面不存在</html>",
        ))

    def test_fetch_fund_info_extracts_datas_node(self) -> None:
        info = fetch_fund_info("000055", fetch_json=lambda _url, _timeout: FUND_INFO_PAYLOAD)
        self.assertEqual(info["purchaseStatusText"], "限大额")
        self.assertEqual(info["maxPurchasePerDay"], 1000.0)
        self.assertEqual(info["minPurchase"], 1.0)
        self.assertEqual(info["fundName"], "纳指ETF联接")
        self.assertEqual(info["fundShares"], 9465110600.0)
        self.assertEqual(info["fundSize"], 19468268721.53)
        self.assertEqual(info["fundSharesAsOf"], "2026-06-30")

    def test_build_limit_payload_maps_status_and_amount(self) -> None:
        info = fetch_fund_info("000055", fetch_json=lambda _url, _timeout: FUND_INFO_PAYLOAD)
        fees = fetch_f10_fees("000055", fetch_text=lambda _url, _timeout: F10_HTML)
        payload = build_limit_payload(info, fees)
        self.assertEqual(payload["code"], "000055")
        self.assertEqual(payload["buyStatus"], "limit_large")
        self.assertEqual(payload["buyStatusText"], "限大额")
        self.assertEqual(payload["maxPurchasePerDay"], 1000.0)
        self.assertEqual(payload["channelLimits"], {"all": 1000.0})
        self.assertEqual(payload["redeemStatus"], "open")
        self.assertEqual(payload["source"], "eastmoney-direct")

    def test_build_limit_payload_suspended_fund(self) -> None:
        payload = build_limit_payload({
            "code": "000055",
            "purchaseStatusText": "暂停申购",
        }, None)
        self.assertEqual(payload["buyStatus"], "suspend")
        self.assertIsNone(payload["maxPurchasePerDay"])

    def test_build_limit_payload_requires_status(self) -> None:
        self.assertIsNone(build_limit_payload({"code": "000055"}, None))
        self.assertIsNone(build_limit_payload(None, None))


if __name__ == "__main__":
    unittest.main()
