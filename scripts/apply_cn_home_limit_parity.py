from pathlib import Path

p = Path('market_collector/aggregates.py')
s = p.read_text()
s = s.replace('return "holiday", "非交易日"', 'return "holiday", "A 股休市"')
s = s.replace('return "pre_open", "A 股盘前"', 'return "pre_open", "A 股待开市"')
start = s.index('    def fund_limit_overview(self) -> dict[str, Any]:')
end = s.index('\n    def dataset_record(', start)
method = '''    def fund_limit_overview(self) -> dict[str, Any]:
        """Build a Mini Program compatible OTC quota snapshot from local history."""
        rows = self.store.read_fund_reference_history("fund_limit", 30)
        empty = {
            "schemaVersion": 1, "limitAsOf": None, "generatedAt": None,
            "coverage": {"covered": 0, "total": 0, "review": 0},
            "currencyTotals": [], "records": [], "trend": [], "events": [],
            "source": "market-collector",
        }
        if not rows:
            return empty

        def status_of(payload: dict[str, Any]) -> str:
            raw = str(payload.get("purchaseStatus") or payload.get("buyStatus") or payload.get("status") or "").strip().lower()
            if raw in {"suspend", "suspended", "paused", "closed"}:
                return "suspended"
            if raw in {"limit_large", "limited", "restricted", "limit"}:
                return "limited"
            if raw in {"open", "available", "normal"}:
                return "open"
            return "unknown"

        def amount_of(payload: dict[str, Any]) -> float | None:
            for key in ("limitAmount", "maxPurchasePerDay", "amount", "purchaseLimit"):
                value = _number(payload.get(key))
                if value is not None and value >= 0:
                    return value
            return None

        def currency_of(payload: dict[str, Any]) -> str:
            value = str(payload.get("currency") or "CNY").strip().upper()
            return value if value in {"CNY", "USD"} else "CNY"

        def name_of(code: str, payload: dict[str, Any]) -> str:
            if code == "022523":
                return "天弘标普500发起(QDII-FOF)D"
            return str(payload.get("fundName") or payload.get("name") or code)

        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            code = str(row.get("symbol") or "")
            if code:
                by_symbol.setdefault(code, []).append(row)
        for snapshots in by_symbol.values():
            snapshots.sort(key=lambda item: (str(item.get("snapshot_date") or ""), str(item.get("fetched_at") or "")))

        latest_rows = [snapshots[-1] for snapshots in by_symbol.values() if snapshots]
        latest_date = max(str(row.get("snapshot_date") or "") for row in latest_rows)
        generated_at = max((str(row.get("fetched_at") or "") for row in latest_rows), default="") or latest_date
        records = []
        for row in latest_rows:
            payload = row.get("payload") or {}
            code = str(row.get("symbol") or payload.get("code") or "")
            status = status_of(payload)
            amount = None if status == "suspended" else amount_of(payload)
            channel = str(payload.get("limitChannel") or "").lower()
            app_label = str(payload.get("limitChannelText") or "")
            if not app_label:
                app_label = "基金公司 App" if channel == "app" else ("销售渠道" if channel == "channel" else "")
            records.append({
                "code": code, "fundName": name_of(code, payload), "name": name_of(code, payload),
                "currency": currency_of(payload), "purchaseStatus": status,
                "buyStatus": str(payload.get("buyStatus") or ""), "limitAmount": _round4(amount),
                "amount": _round4(amount), "isSuspended": status == "suspended",
                "isPending": bool(payload.get("isPending")) or status == "unknown", "appLabel": app_label,
                "channelLimits": payload.get("channelLimits") or {},
            })
        records.sort(key=lambda item: (item["currency"], item["isSuspended"], -(item["amount"] or 0), item["name"]))

        currencies = sorted({item["currency"] for item in records}, key=lambda value: (value != "CNY", value))
        currency_totals = []
        for currency in currencies:
            current = [item for item in records if item["currency"] == currency]
            eligible = [item for item in current if not item["isPending"] and item["purchaseStatus"] != "unknown"]
            limited = [item for item in eligible if item["purchaseStatus"] == "limited"]
            currency_totals.append({"currency": currency, "amount": _round4(sum(item["amount"] or 0 for item in limited)), "fundCount": len(eligible), "limitedCount": len(limited)})

        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_date.setdefault(str(row.get("snapshot_date") or ""), []).append(row)
        trend = []
        for day in sorted(key for key in by_date if key):
            totals: dict[str, float] = {}
            for row in by_date[day]:
                payload = row.get("payload") or {}
                if status_of(payload) != "limited" or bool(payload.get("isPending")):
                    continue
                value = amount_of(payload)
                if value is not None:
                    currency = currency_of(payload)
                    totals[currency] = totals.get(currency, 0.0) + value
            trend.append({"date": day, "cny": _round4(totals.get("CNY")), "usd": _round4(totals.get("USD")), "totalByCurrency": {key: _round4(value) for key, value in totals.items()}, "coveredCount": len(by_date[day])})

        events = []
        for code, snapshots in by_symbol.items():
            for index in range(1, len(snapshots)):
                before = snapshots[index - 1].get("payload") or {}
                after = snapshots[index].get("payload") or {}
                before_status, after_status = status_of(before), status_of(after)
                before_amount = 0 if before_status == "suspended" else amount_of(before)
                after_amount = 0 if after_status == "suspended" else amount_of(after)
                event_type = ""
                if before_status != "suspended" and after_status == "suspended": event_type = "suspend"
                elif before_status == "suspended" and after_status != "suspended": event_type = "resume"
                elif before_amount is not None and after_amount is not None and before_amount != after_amount: event_type = "tighten" if after_amount < before_amount else "relax"
                elif before_status in {"open", "unknown"} and after_status == "limited" and after_amount is not None: event_type = "new_limit"
                if not event_type: continue
                effective_at = after.get("effectiveDate") or snapshots[index].get("snapshot_date")
                events.append({"id": ":".join([str(effective_at or ""), code, event_type]), "type": event_type, "code": code, "name": name_of(code, after), "currency": currency_of(after), "previousAmount": _round4(before_amount), "currentAmount": _round4(after_amount), "effectiveAt": effective_at})
        events.sort(key=lambda item: str(item.get("effectiveAt") or ""), reverse=True)
        review_count = sum(item["purchaseStatus"] in {"limited", "suspended"} for item in records)
        return {
            "schemaVersion": 1, "limitAsOf": latest_date, "generatedAt": generated_at,
            "coverage": {"covered": len(records), "total": len(by_symbol), "review": review_count},
            "currencyTotals": currency_totals, "records": records, "trend": trend,
            "events": events[:100], "source": "market-collector",
        }
'''
p.write_text(s[:start] + method + s[end:])

Path('tests/test_home_limit_parity.py').write_text('''import tempfile\nimport unittest\nfrom market_collector.aggregates import MarketDataService\n\nclass Store:\n    backend_name = "test"\n    def __init__(self, rows): self.rows = rows\n    def read_fund_reference_history(self, kind, days): return self.rows\n\ndef row(code, day, **payload):\n    return {"symbol": code, "snapshot_date": day, "fetched_at": day + "T22:30:00+08:00", "payload": payload}\n\nclass HomeLimitParityTest(unittest.TestCase):\n    def test_multiple_currencies_records_and_events(self):\n        rows = [\n            row("022523", "2026-09-06", fundName="旧名称", currency="CNY", buyStatus="limit_large", maxPurchasePerDay=100),\n            row("022523", "2026-09-07", fundName="旧名称", currency="CNY", buyStatus="limit_large", maxPurchasePerDay=50, limitChannel="app"),\n            row("999999", "2026-09-06", fundName="美元基金", currency="USD", buyStatus="suspend", maxPurchasePerDay=20),\n            row("999999", "2026-09-07", fundName="美元基金", currency="USD", buyStatus="limit_large", maxPurchasePerDay=30),\n        ]\n        result = MarketDataService(Store(rows), tempfile.mkdtemp()).fund_limit_overview()\n        self.assertEqual([x["currency"] for x in result["currencyTotals"]], ["CNY", "USD"])\n        self.assertEqual(len(result["records"]), 2)\n        cny = next(x for x in result["records"] if x["code"] == "022523")\n        self.assertEqual(cny["name"], "天弘标普500发起(QDII-FOF)D")\n        self.assertEqual(cny["appLabel"], "基金公司 App")\n        self.assertEqual(next(x for x in result["currencyTotals"] if x["currency"] == "USD")["amount"], 30)\n        self.assertEqual({x["type"] for x in result["events"]}, {"tighten", "resume"})\n\nif __name__ == "__main__": unittest.main()\n''')
