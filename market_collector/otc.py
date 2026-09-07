"""场外基金采集：蛋卷直连版（不再经过 CF workers）。

每晚定时拉 OTC/QDII 基金详情（净值/涨幅/区间收益/类型），
写入 data 目录的 otc-latest.json 供产品表与 API 兜底使用。
"""
from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .danjuan import build_otc_item, fetch_fund_detail

SHANGHAI = ZoneInfo("Asia/Shanghai")

OTC_SYMBOLS = [
    "000834", "008971", "270042", "006479", "000055", "006480", "021778", "161130",
    "012870", "012871", "003722", "040046", "040047", "040048", "014978", "016055",
    "016057", "016056", "016058", "015299", "015300", "015518", "016532", "016533",
    "016534", "016535", "021838", "018966", "018967", "018968", "018969", "019524",
    "019525", "019547", "019548", "160213", "019172", "019173", "019174", "019175",
    "019441", "019442", "019736", "019737", "019738", "019739", "016452", "016453",
    "021000", "018043", "018044", "022525", "539001", "012751", "012752", "012753",
    "023422", "021773", "022664", "024237", "017641", "019305", "017642", "017643",
    "017028", "017030", "018064", "018065", "018066", "050025", "050030", "006075", "018738",
    "013425", "013499", "007721", "007722", "022523", "161125", "012860", "003718",
    "012861",
]

FetchDetail = Callable[[str, float], dict[str, Any] | None]


def _fetch_one(code: str, timeout_sec: float, fetch_detail: FetchDetail, collected_at: str) -> dict[str, Any] | None:
    detail = fetch_detail(code, timeout_sec)
    if not isinstance(detail, dict):
        raise ValueError("danjuan detail unavailable")
    return build_otc_item(detail, collected_at)


def fetch_otc_metrics(
    symbols: list[str] | None = None,
    timeout_sec: float = 30.0,
    fetch_detail: FetchDetail = fetch_fund_detail,
) -> dict[str, Any]:
    """逐只直连蛋卷详情，输出与旧 worker 版同构的 otc-latest 载荷。"""
    codes = list(dict.fromkeys(symbols or OTC_SYMBOLS))
    collected_at = datetime.now(timezone.utc).astimezone(SHANGHAI).isoformat(timespec="seconds")
    by_code: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    workers = max(1, min(4, len(codes))) if codes else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_one, code, timeout_sec, fetch_detail, collected_at): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                item = future.result()
                if item is None:
                    errors.append(f"{code}: danjuan detail without nav")
                else:
                    by_code[code] = item
            except Exception as exc:
                errors.append(f"{code}: {exc}")
    ordered = [by_code[code] for code in codes if code in by_code]
    return {
        "kind": "market-collector-otc-latest",
        "generated_at": collected_at,
        "requested": len(codes), "success_count": len(ordered),
        "failure_count": len(codes) - len(ordered), "errors": errors, "items": ordered,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
