import json
from collections import defaultdict
from datetime import datetime, timedelta

from reporting import PAID_STATUSES, TZ, _normalized, _parse_date, normalize_installment, vobi_get, vobi_token

PROJECTS = [
    "Centro de Custo - Administrativo Fixo",
    "Centro de Custo - Administrativo Variável",
]
FILTER_KEYS = ["where[refurbishName]", "where[projectName]"]


def _matching_ratio(rows, project):
    if not rows:
        return 0.0
    target = _normalized(project)
    values = [_normalized(row.get("refurbishName") or "") for row in rows]
    return sum(1 for value in values if value == target) / len(values)


def _detect_project_filter(token, project):
    tests = []
    for filter_key in FILTER_KEYS:
        payload = vobi_get(
            "financial/installments",
            token,
            params={"limit": 50, "offset": 0, filter_key: project},
        )
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        count = payload.get("count") if isinstance(payload, dict) else len(rows)
        ratio = _matching_ratio(rows, project)
        tests.append({"filter_key": filter_key, "count": count, "sample_rows": len(rows), "match_ratio": ratio})
        if rows and ratio >= 0.90:
            return filter_key, tests
    return None, tests


def _fetch_project_paid_rows(token, project, filter_key):
    all_rows = []
    meta = []
    for status in sorted(PAID_STATUSES):
        limit = 500
        offset = 0
        status_rows = []
        api_count = None
        while True:
            payload = vobi_get(
                "financial/installments",
                token,
                params={
                    "limit": limit,
                    "offset": offset,
                    filter_key: project,
                    "where[idInstallmentStatus]": status,
                },
            )
            batch = payload.get("rows", []) if isinstance(payload, dict) else []
            if api_count is None and isinstance(payload, dict):
                try:
                    api_count = int(payload.get("count"))
                except (TypeError, ValueError):
                    api_count = None
            status_rows.extend(batch)
            if not batch or len(batch) < limit:
                break
            if api_count is not None and len(status_rows) >= api_count:
                break
            offset += len(batch)
            if offset >= 10000:
                break
        all_rows.extend(status_rows)
        meta.append({"status": status, "api_count": api_count, "fetched": len(status_rows)})
    return all_rows, meta


def previous_month_admin_cash():
    now = datetime.now(TZ)
    today = now.date()
    first_this_month = today.replace(day=1)
    period_end = first_this_month - timedelta(days=1)
    period_start = period_end.replace(day=1)
    token = vobi_token()

    raw_rows = []
    project_queries = []
    for project in PROJECTS:
        filter_key, tests = _detect_project_filter(token, project)
        project_meta = {"project": project, "filter_key": filter_key, "tests": tests}
        if filter_key:
            rows, paid_meta = _fetch_project_paid_rows(token, project, filter_key)
            raw_rows.extend(rows)
            project_meta["paid_meta"] = paid_meta
            project_meta["fetched_rows"] = len(rows)
        else:
            project_meta["fetched_rows"] = 0
        project_queries.append(project_meta)

    seen = set()
    matches = []
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
        project = str(item.get("project") or "Sem obra/projeto")
        if _normalized(project) not in {_normalized(name) for name in PROJECTS}:
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
        "status": "ok" if any(q.get("filter_key") for q in project_queries) else "project_filter_unavailable",
        "generated_at": now.isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "project_queries": project_queries,
        "unique_rows": len(seen),
        "matched_admin_rows": len(matches),
        "income": total_income,
        "expense": total_expense,
        "net": round(total_income - total_expense, 2),
        "projects": projects,
        "items": sorted(matches, key=lambda x: (x["paid_date"], x["project"], -x["amount"])),
    }
    print("ADMIN_PREVIOUS_MONTH_CASH " + json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return result
