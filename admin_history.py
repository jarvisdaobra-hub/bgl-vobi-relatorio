import calendar
import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from reporting import PAID_STATUSES, TZ, _normalized, _parse_date, fetch_installments, normalize_installment, vobi_token


def previous_month_admin_cash():
    now = datetime.now(TZ)
    today = now.date()
    first_this_month = today.replace(day=1)
    period_end = first_this_month - timedelta(days=1)
    period_start = period_end.replace(day=1)

    token = vobi_token()
    raw_rows = []
    query_slices = []
    for month in range(1, period_end.month + 1):
        slice_start = date(period_end.year, month, 1)
        slice_end = date(period_end.year, month, calendar.monthrange(period_end.year, month)[1])
        rows, api_count = fetch_installments(token, slice_start, slice_end)
        raw_rows.extend(rows)
        query_slices.append({
            "start": slice_start.isoformat(),
            "end": slice_end.isoformat(),
            "api_count": api_count,
            "fetched_rows": len(rows),
        })

    seen = set()
    matches = []
    paid_rows_in_period = 0
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
        pnorm = _normalized(project)
        if "administrativo" not in pnorm:
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
        "status": "ok",
        "generated_at": now.isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "query_slices": query_slices,
        "raw_rows": len(raw_rows),
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
