from pathlib import Path

path = Path('market_collector/aggregates.py')
text = path.read_text(encoding='utf-8')

start = text.index('GROUPS = [')
end = text.index('\n\nFetchJson =', start)
new_groups = '''GROUPS = [
    {"key": "all", "label": "全部", "order": 0, "codes": list(SYMBOLS)},
    {
        "key": "nasdaq-100", "label": "纳指 100", "order": 10,
        "codes": ["513870", "513390", "513300", "513110", "513100", "159941", "159696", "159660", "159659", "159632", "159513", "159501", "161130"],
    },
    {"key": "sp500", "label": "标普 500", "order": 20, "codes": ["161125", "513500", "513650", "159612", "159655"]},
    {"key": "us-50", "label": "美国50", "order": 30, "codes": ["159577", "513850"]},
    {"key": "nasdaq-tech", "label": "美国科技", "order": 40, "codes": ["159509", "161128"]},
]'''
text = text[:start] + new_groups + text[end:]

start = text.index('    def home_series(self) -> dict[str, Any]:')
end = text.index('\n    def market_summary(', start)
new_method = '''    def home_series(self) -> dict[str, Any]:
        latest = self._latest_by_symbol()
        samples_by_symbol = {symbol: self._raw_samples(symbol) for symbol in SYMBOLS}
        available_dates = sorted({
            parse_iso(str(sample["collected_at"])).astimezone(SHANGHAI).date().isoformat()
            for samples in samples_by_symbol.values()
            for sample in samples
        })
        trading_date = available_dates[-1] if available_dates else None

        # home_series 集合的生产口径：只取最近交易日，并以 1 分钟桶最后一个样本为准。
        # 不能从 168 小时 raw retention 直接截最后 240 个 5 分钟桶，否则会跨交易日。
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        if trading_date:
            for symbol, samples in samples_by_symbol.items():
                buckets: dict[str, dict[str, Any]] = {}
                for sample in samples:
                    sample_date = parse_iso(str(sample["collected_at"])).astimezone(SHANGHAI).date().isoformat()
                    if sample_date != trading_date:
                        continue
                    bucket = bucket_start_iso(str(sample["collected_at"]), 60)
                    buckets[bucket] = sample
                points = []
                for bucket, sample in sorted(buckets.items(), key=lambda item: parse_iso(item[0]).timestamp()):
                    price = _number(sample.get("price"))
                    if price is None or price <= 0:
                        continue
                    points.append({
                        "time": bucket,
                        "date": trading_date,
                        "price": _round4(price),
                        "nav": _round4(sample.get("iopv")),
                        "premiumPercent": _round4(sample.get("computed_premium_percent")),
                    })
                by_symbol[symbol] = points
        else:
            by_symbol = {symbol: [] for symbol in SYMBOLS}

        group_for = {code: group["key"] for group in GROUPS[1:] for code in group["codes"]}
        price_series = []
        premium_series = []
        for symbol in SYMBOLS:
            rows = by_symbol.get(symbol) or []
            if not rows:
                continue
            name = (latest.get(symbol) or {}).get("name") or symbol
            group_key = group_for.get(symbol, "all")
            price_series.append({
                "key": symbol, "code": symbol, "name": name, "groupKey": group_key,
                "points": [{"time": row["time"], "price": row["price"]} for row in rows],
            })
            premium_points = [{
                "time": row["time"], "price": row["price"], "nav": row["nav"],
                "premiumPercent": row["premiumPercent"], "navDate": trading_date,
            } for row in rows if row["premiumPercent"] is not None]
            if premium_points:
                premium_series.append({
                    "key": symbol, "code": symbol, "name": name, "groupKey": group_key,
                    "points": premium_points,
                })

        def aggregate_points(group: dict[str, Any], metric: str) -> list[dict[str, Any]]:
            values: dict[str, list[float]] = defaultdict(list)
            bases: dict[str, float] = {}
            for code in group["codes"]:
                for row in by_symbol.get(code) or []:
                    value = _number(row["price"] if metric == "price" else row["premiumPercent"])
                    if value is None:
                        continue
                    if metric == "price":
                        bases.setdefault(code, value)
                        value = value / bases[code] * 100
                    values[row["time"]].append(value)
            return [
                {"time": key, "value": _round4(statistics.mean(items) if metric == "price" else statistics.median(items))}
                for key, items in sorted(values.items()) if items
            ]

        price_aggregates = [{
            "key": f"price-equal-weight-{group['key']}", "role": "equal_weight",
            "label": group["label"] + "等权", "groupKey": group["key"],
            "normalized": True, "points": aggregate_points(group, "price"),
        } for group in GROUPS]
        premium_aggregates = [{
            "key": f"premium-median-{group['key']}", "role": "median",
            "label": group["label"] + "中位数", "groupKey": group["key"],
            "points": aggregate_points(group, "premium"),
        } for group in GROUPS]

        yesterday = None
        if len(available_dates) > 1:
            previous_date = available_dates[-2]
            previous_values = []
            for samples in samples_by_symbol.values():
                matching = [sample for sample in samples if parse_iso(str(sample["collected_at"])).astimezone(SHANGHAI).date().isoformat() == previous_date]
                if not matching:
                    continue
                value = _number(matching[-1].get("computed_premium_percent"))
                if value is not None:
                    previous_values.append(value)
            if previous_values:
                yesterday = {
                    "premiumMedianPercent": _round4(statistics.median(previous_values)),
                    "tradingDate": previous_date,
                }

        return {
            "schemaVersion": 1, "tradingDate": trading_date,
            "bucketMinutes": 1, "windowLabel": "今日 · 1 分钟", "defaultGroupKey": "all",
            "generatedAt": _shanghai_iso(datetime.now(timezone.utc)),
            "groups": [{key: value for key, value in group.items() if key != "codes"} for group in GROUPS],
            "yesterday": yesterday,
            "modes": {
                "price": {"aggregate": {"series": price_aggregates}, "series": price_series},
                "premium": {"aggregate": {"series": premium_aggregates}, "series": premium_series},
            },
            "source": f"market-collector-{self.store.backend_name}-1m",
        }
'''
text = text[:start] + new_method + text[end:]
path.write_text(text, encoding='utf-8')

# Add focused regression coverage.
test_path = Path('tests/test_aggregates.py')
test = test_path.read_text(encoding='utf-8')
anchor = '    def test_daily_combines_price_nav_and_t_minus_one_premium(self) -> None:\n'
case = '''    def test_home_series_uses_latest_day_one_minute_and_complete_groups(self) -> None:
        older = (datetime.fromisoformat(RECENT_DAY) - timedelta(days=1)).date().isoformat()
        old = record(f"{older}T14:59:00+08:00", 1.9, 1.8, 5.5555)
        self.store.write_cycle([old], 168, 14)

        payload = self.service.home_series()

        self.assertEqual(payload["tradingDate"], RECENT_DAY)
        self.assertEqual(payload["bucketMinutes"], 1)
        self.assertEqual(
            [group["key"] for group in payload["groups"]],
            ["all", "nasdaq-100", "sp500", "us-50", "nasdaq-tech"],
        )
        series = next(item for item in payload["modes"]["premium"]["series"] if item["code"] == "513100")
        self.assertEqual(len(series["points"]), 3)
        self.assertTrue(all(point["time"].startswith(RECENT_DAY) for point in series["points"]))
        self.assertEqual(payload["yesterday"]["tradingDate"], older)
        self.assertEqual(payload["yesterday"]["premiumMedianPercent"], 5.5555)

'''
if anchor not in test:
    raise SystemExit('test anchor not found')
test = test.replace(anchor, case + anchor, 1)
test_path.write_text(test, encoding='utf-8')
