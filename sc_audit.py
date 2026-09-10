from datetime import date, datetime

from reporting import TZ, _normalized, normalize_installment, vobi_get, vobi_token


START = date(2026, 9, 9)
END = date(2026, 9, 30)


def _all_projects(token):
    payload = vobi_get("refurbish", token, params={"limit": 500, "offset": 0})
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    return rows


def _sc_projects(token):
    matches = []
    for row in _all_projects(token):
        name = str(row.get("name") or "").strip()
        text = _normalized(name)
        if any(term in text for term in ("blumenau", "sao jose", "eletrobras", "axia")):
            matches.append({"id": row.get("id"), "name": name, "deletedAt": row.get("deletedAt")})
    return matches


def _fetch_project_rows(token, project_id):
    rows = []
    limit = 500
    offset = 0
    reported_count = None
    while True:
        payload = vobi_get(
            "financial/installments",
            token,
            params={"limit": limit, "offset": offset, "where[idRefurbish]": project_id},
        )
        if reported_count is None and isinstance(payload, dict):
            reported_count = payload.get("count")
        batch = payload.get("rows", []) if isinstance(payload, dict) else []
        rows.extend(batch)
        if not batch or len(batch) < limit:
            break
        offset += len(batch)
        if offset >= 10000:
            break
    return rows, reported_count


def _view(item):
    return {
        "due_date": item["due_date"].isoformat() if item.get("due_date") else None,
        "project": item.get("project"),
        "supplier": item.get("counterparty"),
        "description": item.get("description"),
        "amount": item.get("amount"),
        "ignored": item.get("ignored"),
        "status": item.get("status"),
    }


def _audit_project(token, project):
    raw_rows, reported_count = _fetch_project_rows(token, project["id"])
    items = [normalize_installment(row, START) for row in raw_rows]
    expenses = [
        item for item in items
        if item.get("bill_type") == "expense"
        and not item.get("paid")
        and not item.get("cancelled")
        and item.get("due_date")
        and START <= item["due_date"] <= END
    ]
    expenses.sort(key=lambda x: (x["due_date"], -x["amount"], x["description"]))
    return {
        "project_id": project["id"],
        "project_name": project["name"],
        "reported_count": reported_count,
        "fetched_rows": len(raw_rows),
        "open_expense_count": len(expenses),
        "open_expense_total": sum(float(x.get("amount") or 0) for x in expenses),
        "items": [_view(x) for x in expenses],
    }


def build_sc_audit():
    now = datetime.now(TZ)
    token = vobi_token()
    projects = _sc_projects(token)
    audits = [_audit_project(token, p) for p in projects]
    return {
        "generated_at": now.isoformat(),
        "period_start": START.isoformat(),
        "period_end": END.isoformat(),
        "matched_projects": projects,
        "audits": audits,
    }
