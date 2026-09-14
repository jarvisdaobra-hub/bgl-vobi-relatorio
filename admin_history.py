import json
from collections import defaultdict
from datetime import datetime, timedelta

from reporting import PAID_STATUSES, TZ, _normalized, _parse_date, normalize_installment, vobi_get, vobi_token

PROJECTS = [
    "Centro de Custo - Administrativo Fixo",
    "Centro de Custo - Administrativo Variável",
]

DATE_FILTER_CANDIDATES = [
    ("where[initialPaidDate]", "where[endPaidDate]"),
    ("where[initialPaymentDate]", "where[endPaymentDate]"),
    ("where[paidInitialDate]", "where[paidEndDate]"),
    ("where[paymentInitialDate]", "where[paymentEndDate]"),
    ("where[paidDateFrom]", "where[paidDateTo]"),
    ("where[paymentDateFrom]", "where[paymentDateTo]"),
    ("where[paidDate][gte]", "where[paidDate][lte]"),
    ("where[paidDate][$gte]", "where[paidDate][$lte]"),
    ("initialPaidDate", "endPaidDate"),
    ("paidDateFrom", "paidDateTo"),
]


def _paid_date_ratio(rows, start, end):
    if not rows:
        return 0.0, None, None
    dates = [_parse_date(row.get("paidDate")) for row in rows]
    actual = [d for d in dates if d is not None]
    inside = [d for d in actual if start <= d <= end]
    min_date = min(actual).isoformat() if actual else None
    max_date = max(actual).isoformat() if actual else None
    return (len(inside) / len(rows)), min_date, max_date


def _detect_paid_date_filter(token, start, end):
    tests = []
    for start_key, end_key in DATE_FILTER_CANDIDATES:
        payload = vobi_get(
            "financial/installments",
            token,
            params={
                "limit": 100,
                "offset": 0,
                start_key: start.isoformat(),
                end_key: end.isoformat(),
            },
        )
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        count = payload.get("count") if isinstance(payload, dict) else len(rows)
        ratio, min_date, max_date = _paid_date_ratio(rows, start, end)
        tests.append({
            "start_key": start_key,
            "end_key": end_key,
            "count": count,
            "sample_rows": len(rows),
            "paid_date_match_ratio": ratio,
            "sample_min_paid_date": min_date,
            "sample_max_paid_date": max_date,
        })
        if rows and ratio >= 0.90:
            return (start_key, end_key), tests
    return None, tests


def _fetch_period_rows(token, start, end, date_filter):
    start_key, end_key = date_filter
    rows = []
    limit = 500
    offset = 0
    api_count = None
    while True:
        payload = vobi_get(
            "financial/installments",
            token,
            params={
                "limit": limit,
                "offset": offset,
                start_key: start.isoformat(),
                end_key: end.isoformat(),
            },
        )
        batch = payload.get("rows", []) if isinstance(payload, dict) else []
        if api_count is None and isinstance(payload, dict):
            try:
                api_count = int(payload.get("count"))
            except (TypeError, ValueError):
                api_count = None
        rows.extend(batch)
        if not batch or len(batch) < limit:
            break
        if api_count is not None and len(rows) >= api_count:
            break
        offset += len(batch)
        if offset >= 10000:
            break
    return rows, api_count


def previous_month_admin_cash():
    now = datetime.now(TZ)
    today = now.date()
    first_this_month = today.replace(day=1)
    period_end = first_this_month - timedelta(days=1)
    period_start = period_end.replace(day=1)
    token = vobi_token()

    date_filter, filter_tests = _detect_paid_date_filter(token, period_start, period_end)
    raw_rows = []
    api_count = None
    if date_filter:
        raw_rows, api_count = _fetch_period_rows(token, period_start, period_end, date_filter)

    seen = set()
    matches = []
    paid_rows_in_period = 0
    project_targets = {_normalized(name) for name in PROJECTS}

    for row in raw_rows:
        row_id = row.get("id")
        key = str(row_id) if row_id is not None else "|".join(
            str(row.get(k) or "") for k in ("paidDate", "number", "price", "totalSplitPrice", "refurbishName", "description")
        )
        if key in seen:
            continue
        seen.add(key)

        paid_date = _parse_date(row.get("paidDate"))
        if not paid_date or paid_date < period_start or paid_date > period_end:
            continue
        item = normalize_installment(row, today)
        if item.get("status") not in PAID_STATUSES:
            continue
        paid_rows_in_period += 1

        project = str(item.get("project") or "Sem obra/projeto")
        if _normalized(project) not in project_targets:
            continue
        bill_type = item.get("bill_type")
        if bill_type not in {"income", "expense"}:
            continue

        matches.append({
            "id": row_id,
            "paid_date": paid_date.isoformat(),
            "project": project,
            "bill_type": bill_type,
            "counterparty": item.get("counterparty"),
            "description": item.get("description"),
            "amount": float(item.get("amount") or 0.0),
        })

    by_project = defaultdict(lambda: {"income": 0.0, "expense": 0.0, "income_count": 0, "expense_count": 0})
    for item in matches:
        bucket = by_project[item["project"]]
        bucket[item["bill_type"]] += item["amount"]
        bucket[item["bill_type"] + "_count"] += 1

    projects = []
    for project, values in by_project.items():
        projects.append({
            "project": project,
            "income": round(values["income"], 2),
            "expense": round(values["expense"], 2),
            "net": round(values["income"] - values["expense"], 2),
            "income_count": values["income_count"],
            "expense_count": values["expense_count"],
        })
    projects.sort(key=lambda x: x["expense"], reverse=True)

    total_income = round(sum(x["income"] for x in projects), 2)
    total_expense = round(sum(x["expense"] for x in projects), 2)
    result = {
        "status": "ok" if date_filter else "paid_date_filter_unavailable",
        "generated_at": now.isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "date_filter": date_filter,
        "filter_tests": filter_tests,
        "api_count": api_count,
        "fetched_rows": len(raw_rows),
        "unique_rows": len(seen),
        "paid_rows_in_period": paid_rows_in_period,
        "matched_admin_rows": len(matches),
        "income": total_income,
        "expense": total_expense,
        "net": round(total_income - total_expense, 2),
        "projects": projects,
        "items": sorted(matches, key=lambda x: (x["paid_date"], x["project"], -x["amount"])),
    }
    print("ADMIN_PREVIOUS_MONTH_CASH " + json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return result
