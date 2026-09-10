import json
from datetime import datetime

from reporting import TZ, _normalized, normalize_installment, vobi_get, vobi_token


def _view(item):
    return {
        "date": item["effective_date"].isoformat() if item.get("effective_date") else None,
        "due_date": item["due_date"].isoformat() if item.get("due_date") else None,
        "project": item.get("project"),
        "counterparty": item.get("counterparty"),
        "description": item.get("description"),
        "amount": item.get("amount"),
        "ignored": item.get("ignored"),
        "is_sc": item.get("is_sc"),
    }


def _sample(token, extra):
    try:
        payload = vobi_get("financial/installments", token, params={"limit": 50, "offset": 0, **extra})
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        return {
            "count": payload.get("count") if isinstance(payload, dict) else None,
            "rows": len(rows),
            "projects": sorted({str(r.get("refurbishName") or "") for r in rows if r.get("refurbishName")})[:12],
            "descriptions": [str(r.get("description") or "")[:80] for r in rows[:5]],
            "due_dates": [str(r.get("dueDate") or "")[:10] for r in rows[:10]],
        }
    except Exception as exc:
        return {"error": type(exc).__name__}


def _probe_project_filters(token):
    exact = "Execução - Eletrobrás Blumenau/SC"
    variants = {
        "search_blumenau": {"search": "Blumenau"},
        "q_blumenau": {"q": "Blumenau"},
        "query_blumenau": {"query": "Blumenau"},
        "term_blumenau": {"term": "Blumenau"},
        "name_blumenau": {"name": "Blumenau"},
        "refurbishName_exact": {"refurbishName": exact},
        "projectName_exact": {"projectName": exact},
        "where_refurbishName_exact": {"where[refurbishName]": exact},
        "where_projectName_exact": {"where[projectName]": exact},
        "where_refurbishName_like": {"where[refurbishName][like]": "%Blumenau%"},
        "where_refurbishName_dollar_like": {"where[refurbishName][$like]": "%Blumenau%"},
        "filter_refurbishName": {"filter[refurbishName]": exact},
        "json_where_refurbishName": {"where": json.dumps({"refurbishName": exact})},
        "json_filter_refurbishName": {"filter": json.dumps({"where": {"refurbishName": exact}})},
    }
    return {name: _sample(token, params) for name, params in variants.items()}


def _probe_order(token):
    variants = {
        "order_due_desc": {"order": "dueDate DESC"},
        "order_due_colon_desc": {"order": "dueDate:desc"},
        "sort_due_desc": {"sort": "dueDate", "direction": "desc"},
        "sortBy_due_desc": {"sortBy": "dueDate", "sortDirection": "desc"},
        "orderBy_due_desc": {"orderBy": "dueDate", "orderDirection": "desc"},
        "order_created_desc": {"order": "createdAt DESC"},
        "sort_created_desc": {"sort": "createdAt", "direction": "desc"},
    }
    return {name: _sample(token, params) for name, params in variants.items()}


def build_sc_audit():
    now = datetime.now(TZ)
    token = vobi_token()
    return {
        "generated_at": now.isoformat(),
        "project_filter_probe": _probe_project_filters(token),
        "order_probe": _probe_order(token),
    }
