from pathlib import Path

p = Path('market_collector/aggregates.py')
s = p.read_text()
marker = ']\n\nFetchJson = Callable'
home_codes = '''

HOME_BREADTH_SYMBOLS = {
    "513870", "513390", "513300", "513110", "513100", "159941", "159696", "159660",
    "159659", "159632", "159513", "159509", "159501", "159577", "161128", "161130",
    "513500", "513650", "159612", "159655", "513850",
}
'''
if 'HOME_BREADTH_SYMBOLS' not in s:
    s = s.replace(marker, ']' + home_codes + '\nFetchJson = Callable', 1)
s = s.replace('        records = latest.get("symbols") or []\n        premiums =', '        records = [\n            item for item in (latest.get("symbols") or [])\n            if str(item.get("symbol") or "") in HOME_BREADTH_SYMBOLS\n        ]\n        premiums =', 1)
s = s.replace('        total = len(SYMBOLS)\n        market_state, session_label = _market_state()', '        total = len(HOME_BREADTH_SYMBOLS)\n        market_state, session_label = _market_state()', 1)
helper = '''    @staticmethod
    def _normalize_limit_snapshot(source: dict[str, Any]) -> dict[str, Any]:
        summary = source.get("summary") or {}
        raw_records = source.get("records") if isinstance(source.get("records"), list) else []

        def num(value: Any) -> float | None:
            value = _number(value)
            return value if value is not None and value >= 0 else None

        def status_of(item: dict[str, Any]) -> str:
            raw = str(item.get("purchaseStatus") or item.get("buyStatus") or item.get("status") or "").lower()
            if raw in {"suspend", "suspended", "paused", "closed"}: return "suspended"
            if raw in {"limit_large", "limited", "restricted", "limit"}: return "limited"
            if raw in {"open", "available", "normal", "unlimited"}: return "open"
            return "unknown"

        records = []
        by_code: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(raw_records):
            item = dict(raw) if isinstance(raw, dict) else {}
            code = str(item.get("code") or item.get("symbol") or item.get("fundCode") or f"fund-{index + 1}")
            name = "天弘标普500发起(QDII-FOF)D" if code == "022523" else str(item.get("fundName") or item.get("name") or code)
            currency = str(item.get("currency") or "CNY").upper()
            if currency not in {"CNY", "USD"}: currency = "CNY"
            status = status_of(item)
            amount = num(item.get("limitAmount"))
            if amount is None: amount = num(item.get("maxPurchasePerDay"))
            if status == "suspended": amount = 0
            channel = str(item.get("purchaseChannel") or item.get("limitChannel") or "")
            channel_text = str(item.get("purchaseChannelText") or item.get("limitChannelText") or "")
            app_label = "App购买" if any(word in (channel + " " + channel_text) for word in ("直销", "APP", "App", "app", "官网", "微信公众", "直销柜台")) else ""
            normalized = {**item, "code": code, "name": name, "fundName": name, "currency": currency,
                          "purchaseStatus": status, "amount": _round4(amount), "limitAmount": _round4(amount),
                          "isSuspended": status == "suspended", "isPending": bool(item.get("isPending")),
                          "appLabel": app_label}
            records.append(normalized)
            by_code[code] = normalized

        raw_totals = summary.get("totalByCurrency") if isinstance(summary.get("totalByCurrency"), dict) else {}
        currencies = sorted(set(raw_totals) | {item["currency"] for item in records}, key=lambda value: (value != "CNY", value))
        currency_totals = []
        for currency in currencies:
            current = [item for item in records if item["currency"] == currency]
            total_value = num(raw_totals.get(currency))
            if total_value is None:
                total_value = sum(item.get("amount") or 0 for item in current if item["purchaseStatus"] == "limited" and not item["isPending"])
            currency_totals.append({"currency": currency, "amount": _round4(total_value),
                                    "fundCount": sum(not item["isPending"] and item["purchaseStatus"] != "unknown" for item in current),
                                    "limitedCount": sum(not item["isPending"] and item["purchaseStatus"] == "limited" for item in current)})

        trend = []
        for item in source.get("trend") or []:
            if not isinstance(item, dict): continue
            totals = item.get("totalByCurrency") if isinstance(item.get("totalByCurrency"), dict) else {}
            trend.append({"date": item.get("date"), "cny": _round4(totals.get("CNY")), "usd": _round4(totals.get("USD")),
                          "totalByCurrency": totals, "coveredCount": int(item.get("coveredFundCount") or item.get("coveredCount") or 0)})

        def event_amount(side: Any) -> float | None:
            if isinstance(side, dict): return num(side.get("limitAmount") if side.get("limitAmount") is not None else side.get("amount"))
            return num(side)

        events = []
        raw_events = source.get("recentEvents") if isinstance(source.get("recentEvents"), list) else source.get("events") or []
        for index, raw in enumerate(raw_events):
            if not isinstance(raw, dict): continue
            before_obj = raw.get("before") if isinstance(raw.get("before"), dict) else {}
            after_obj = raw.get("after") if isinstance(raw.get("after"), dict) else {}
            code = str(raw.get("code") or ((after_obj.get("codes") or [""])[0] if isinstance(after_obj.get("codes"), list) else "") or ((before_obj.get("codes") or [""])[0] if isinstance(before_obj.get("codes"), list) else ""))
            record = by_code.get(code) or {}
            event_type = str(raw.get("type") or "scope_changed")
            before = num(raw.get("previousAmount"))
            after = num(raw.get("currentAmount"))
            if before is None: before = event_amount(raw.get("before"))
            if after is None: after = event_amount(raw.get("after"))
            if event_type == "resume": before = 0
            currency = str(raw.get("currency") or after_obj.get("currency") or before_obj.get("currency") or record.get("currency") or "CNY").upper()
            events.append({"id": raw.get("id") or f"{source.get('asOf') or ''}:{code or index}:{event_type}", "type": event_type,
                           "code": code, "name": raw.get("fundName") or record.get("name") or code, "currency": currency,
                           "previousAmount": _round4(before), "currentAmount": _round4(after),
                           "effectiveAt": raw.get("effectiveAt") or raw.get("observedAt") or source.get("asOf")})

        covered = int(summary.get("coveredFundCount") or len(records))
        total = int(summary.get("expectedFundCount") or len(records))
        return {"schemaVersion": 1, "limitAsOf": source.get("asOf"), "generatedAt": source.get("asOf"),
                "coverage": {"covered": covered, "total": total, "review": int(summary.get("reviewCount") or 0)},
                "currencyTotals": currency_totals, "records": records, "trend": trend, "events": events[:100],
                "source": "market-collector-local-snapshot"}

'''
method_marker = '    def fund_limit_overview(self) -> dict[str, Any]:\n'
if '_normalize_limit_snapshot' not in s:
    s = s.replace(method_marker, helper + method_marker, 1)
load_block = '''        snapshot_path = self.data_dir / "fund-limit-overview.json"
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(snapshot.get("records"), list) and snapshot.get("records"):
                return self._normalize_limit_snapshot(snapshot)
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
'''
if 'snapshot_path = self.data_dir / "fund-limit-overview.json"' not in s:
    s = s.replace(method_marker, method_marker + load_block, 1)
p.write_text(s)

p = Path('market_collector/core.py')
s = p.read_text()
old = '        return self.fund_store.upsert_limit_overview(payload)\n'
new = '''        atomic_write_json(Path(str(self.config["output_dir"])) / "fund-limit-overview.json", payload)
        return self.fund_store.upsert_limit_overview(payload)
'''
if 'fund-limit-overview.json' not in s:
    s = s.replace(old, new, 1)
p.write_text(s)

Path('tests/test_home_snapshot_parity.py').write_text('''import json\nimport tempfile\nimport unittest\nfrom pathlib import Path\nfrom market_collector.aggregates import HOME_BREADTH_SYMBOLS, MarketDataService\n\nclass Store:\n    backend_name = "test"\n    def read_fund_reference_history(self, kind, days): return []\n\nclass HomeSnapshotParityTest(unittest.TestCase):\n    def test_breadth_only_counts_mini_program_pool(self):\n        folder = Path(tempfile.mkdtemp())\n        symbols = [{"symbol": code, "change_percent": 1, "computed_premium_percent": 2, "price": 1} for code in HOME_BREADTH_SYMBOLS]\n        symbols += [{"symbol": "019736", "change_percent": -1, "computed_premium_percent": 9, "price": 1}]\n        (folder / "latest.json").write_text(json.dumps({"generated_at": "2026-09-08T10:30:00+08:00", "symbols": symbols}))\n        result = MarketDataService(Store(), folder).home_overview()\n        self.assertEqual(result["breadth"]["riseCount"], len(HOME_BREADTH_SYMBOLS))\n        self.assertEqual(result["breadth"]["fallCount"], 0)\n        self.assertEqual(result["coverage"]["price"]["total"], 21)\n\n    def test_limit_snapshot_matches_collection_contract(self):\n        folder = Path(tempfile.mkdtemp())\n        snapshot = {"asOf": "2026-09-08T08:14:00+08:00", "summary": {"totalByCurrency": {"CNY": 1670, "USD": 72}, "coveredFundCount": 2, "expectedFundCount": 2}, "records": [{"code": "019736", "fundName": "宝盈纳斯达克100", "currency": "CNY", "purchaseStatus": "limited", "limitAmount": 200}], "trend": [{"date": "2026-09-07", "totalByCurrency": {"CNY": 1400, "USD": 46}}, {"date": "2026-09-08", "totalByCurrency": {"CNY": 1670, "USD": 72}}], "recentEvents": [{"type": "relax", "code": "019736", "fundName": "宝盈纳斯达克100", "before": 10, "after": 200, "effectiveAt": "2026-09-08"}]}\n        (folder / "fund-limit-overview.json").write_text(json.dumps(snapshot))\n        result = MarketDataService(Store(), folder).fund_limit_overview()\n        self.assertEqual([x["amount"] for x in result["currencyTotals"]], [1670, 72])\n        self.assertEqual(result["events"][0]["type"], "relax")\n        self.assertEqual(result["events"][0]["previousAmount"], 10)\n\nif __name__ == "__main__": unittest.main()\n''')
