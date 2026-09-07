"""基金参考数据同步（直连东财版，不再回源 CF workers）。

- fund_fee：东财 F10 费率页（管理费/托管费/销售服务费 + 申赎状态文案）；
- fund_limit：东财移动端 FundMNBasicInformation（SGZT 申购状态、MINSG/MAXSG 限额），
  辅以 F10 的申赎状态文案做交叉校验。

夜间同步后写入 fund_reference_snapshots（source 前缀 direct:*），
API 的 /fund-fee、/fund-limit 从本地快照读取。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .eastmoney_fund import build_limit_payload, fetch_f10_fees, fetch_fund_info

SHANGHAI = ZoneInfo("Asia/Shanghai")
LIMIT_SCHEMA_VERSION = 2


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) and number > 0 else None


def normalize_limit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把直连限额数据规范化为 collector 的正式 payload（与旧 worker 版同构）。"""
    data = dict(payload or {})
    raw_limits = data.get("channelLimits")
    limits: dict[str, float] = {}
    if isinstance(raw_limits, dict):
        for key in ("direct", "distributor", "all"):
            value = _positive_number(raw_limits.get(key))
            if value is not None:
                limits[key] = value
    if not limits:
        amount = _positive_number(data.get("maxPurchasePerDay"))
        if amount is not None:
            limits["all"] = amount
    if limits:
        data["channelLimits"] = limits
        primary = limits.get("direct") or limits.get("all") or limits.get("distributor")
        if primary is not None:
            data["maxPurchasePerDay"] = primary
        if limits.get("direct") is not None:
            data["limitChannel"] = "app"
        elif limits.get("distributor") is not None:
            data["limitChannel"] = "channel"
        else:
            data["limitChannel"] = data.get("limitChannel") or "channel"
    data["limitSchemaVersion"] = LIMIT_SCHEMA_VERSION
    return data


def normalize_fund_code(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(character for character in text if character.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


FetchFees = Callable[[str, float], dict[str, Any] | None]
FetchInfo = Callable[[str, float], dict[str, Any] | None]


def _snapshot_record(
    data_kind: str,
    code: str,
    payload: dict[str, Any],
    fetched_at: str,
    snapshot_date: str,
) -> dict[str, Any]:
    normalized_payload = dict(payload)
    normalized_payload["code"] = code
    return {
        "data_kind": data_kind,
        "symbol": code,
        "snapshot_date": snapshot_date,
        "fetched_at": fetched_at,
        "source": "direct:" + data_kind.replace("_", "-"),
        "payload": normalized_payload,
    }


def _build_fee_payload(
    code: str,
    fee_data: dict[str, Any],
    fund_name: str | None,
    fetched_at: str,
) -> dict[str, Any]:
    return {
        "annualFeeRate": fee_data.get("annualFeeRate"),
        "code": code,
        "custodyFeeRate": fee_data.get("custodyFeeRate"),
        "fetchedAt": fetched_at,
        "fundName": fund_name,
        "managementFeeRate": fee_data.get("managementFeeRate"),
        "operationFeeRate": fee_data.get("operationFeeRate"),
        "operationFees": fee_data.get("operationFees") or [],
        "purchaseRules": fee_data.get("purchaseRules") or [],
        "redeemRules": fee_data.get("redeemRules") or [],
        "salesServiceFeeRate": fee_data.get("salesServiceFeeRate"),
        "source": "eastmoney-f10",
    }


def _fetch_code_records(
    code: str,
    timeout_sec: float,
    fetched_at: str,
    snapshot_date: str,
    want_fee: bool,
    want_limit: bool,
    fetch_fees: FetchFees,
    fetch_info: FetchInfo,
) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    fee_data: dict[str, Any] | None = None
    info: dict[str, Any] | None = None
    if want_fee:
        fee_data = fetch_fees(code, timeout_sec)
        if not isinstance(fee_data, dict):
            errors.append(f"fund_fee:{code}: eastmoney f10 unavailable")
    if want_limit:
        info = fetch_info(code, timeout_sec)
        if not isinstance(info, dict):
            errors.append(f"fund_limit:{code}: eastmoney fund info unavailable")
    fund_name = str((info or {}).get("fundName") or "") or None
    if want_fee:
        if isinstance(fee_data, dict):
            records.append(_snapshot_record(
                "fund_fee", code,
                _build_fee_payload(code, fee_data, fund_name, fetched_at),
                fetched_at, snapshot_date,
            ))
    if want_limit:
        limit_payload = build_limit_payload(info, fee_data)
        if limit_payload is not None:
            normalized = normalize_limit_payload(limit_payload)
            if fund_name:
                normalized["fundName"] = fund_name
            records.append(_snapshot_record(
                "fund_limit", code, normalized, fetched_at, snapshot_date,
            ))
        elif not any(error.startswith(f"fund_limit:{code}:") for error in errors):
            errors.append(f"fund_limit:{code}: no purchase status from eastmoney")
    return records, errors


def fetch_fund_references(
    symbols: list[str],
    *,
    timeout_sec: float = 25.0,
    concurrency: int = 4,
    now: datetime | None = None,
    fee_symbols: list[str] | None = None,
    limit_symbols: list[str] | None = None,
    fetch_fees: FetchFees = fetch_f10_fees,
    fetch_info: FetchInfo = fetch_fund_info,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI)
    fetched_at = current.replace(microsecond=0).isoformat()
    snapshot_date = current.date().isoformat()
    # 默认 fee/limit 同 symbols；分别传入时可拆开：场内 ETF 只抓 fee（持有成本费率），
    # 场外 OTC 两样都抓（fee 含卖出费率 redeemRules，limit 含限购额度）。
    fee_codes = list(dict.fromkeys(
        code for code in (normalize_fund_code(symbol) for symbol in (fee_symbols or symbols)) if code
    ))
    limit_codes = list(dict.fromkeys(
        code for code in (normalize_fund_code(symbol) for symbol in (limit_symbols or symbols)) if code
    ))
    fee_set = set(fee_codes)
    limit_set = set(limit_codes)
    all_codes = list(dict.fromkeys(fee_codes + limit_codes))
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    if all_codes:
        workers = max(1, min(int(concurrency), 8, len(all_codes)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _fetch_code_records, code, timeout_sec, fetched_at, snapshot_date,
                    code in fee_set, code in limit_set, fetch_fees, fetch_info,
                ): code
                for code in all_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    code_records, code_errors = future.result()
                except Exception as exc:
                    errors.append(f"{code}: {exc}")
                    continue
                records.extend(code_records)
                errors.extend(code_errors)
    fee_records = [record for record in records if record["data_kind"] == "fund_fee"]
    limit_records = [record for record in records if record["data_kind"] == "fund_limit"]
    return {
        "kind": "market-collector-fund-reference-sync",
        "generated_at": fetched_at,
        "snapshot_date": snapshot_date,
        "requested_symbols": len(all_codes),
        "fee_success_count": len(fee_records),
        "fee_failure_count": len(fee_codes) - len(fee_records),
        "limit_success_count": len(limit_records),
        "limit_failure_count": len(limit_codes) - len(limit_records),
        "records": records,
        "errors": errors,
    }
