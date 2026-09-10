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


def _probe_filters(token, start, end):
    s = start.isoformat()
    e = end.isoformat()
    variants = {
        "where_initial_end": {"where[initialDate]": s, "where[endDate]": e},
        "plain_initial_end": {"initialDate": s, "endDate": e},
        "where_due_gte_lte": {"where[dueDate][gte]": s, "where[dueDate][lte]": e},
        "where_due_dollar": {"where[dueDate][$gte]": s, "where[dueDate][$lte]": e},
        "where_due_between": {"where[dueDate][between][0]": s, "where[dueDate][between][1]": e},
        "filter_where_due_between": {"filter[where][dueDate][between][0]": s, "filter[where][dueDate][between][1]": e},
        "json_filter": {"filter": json.dumps({"where": {"dueDate": {"between": [s, e]}}})},
        "plain_start_end": {"startDate": s, "endDate": e},
    }
    results = {}
    chosen = None
    for name, extra in variants.items():
        try:
            params = {"limit": 20, "offset": 0, **extra}
            payload = vobi_get("financial/installments", token, params=params)
            batch = payload.get("rows", []) if isinstance(payload, dict) else []
            dues = [str(r.get("dueDate") or "")[:10] for r in batch]
            results[name] = {
                "count": payload.get("count") if isinstance(payload, dict) else None,
                "rows": len(batch),
                "due_dates": dues,
            }
            valid_dues = [d for d in dues if d]
            if chosen is None and valid_dues and all(s <= d <= e for d in valid_dues):
                chosen = name
        except Exception as exc:
            results[name] = {"error": type(exc).__name__}
    return results, chosen, variants


def _fetch_filtered(token, params_extra):
    limit = 500
    offset = 0
    rows = []
    reported_count = None
    while len(rows) < 10000:
        payload = vobi_get("financial/installments", token, params={"limit": limit, "offset": offset, **params_extra})
        batch = payload.get("rows", []) if isinstance(payload, dict) else []
        if reported_count is None and isinstance(payload, dict):
            reported_count = payload.get("count")
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            break
    return rows, reported_count


def build_sc_audit():
    now = datetime.now(TZ)
    today = now.date()
    end = datetime(2026, 9, 30).date()
    token = vobi_token()
    probes, chosen, variants = _probe_filters(token, today, end)
    chosen_params = variants.get(chosen) if chosen else {"where[initialDate]": today.isoformat(), "where[endDate]": end.isoformat()}
    rows, api_count = _fetch_filtered(token, chosen_params)
    items = [normalize_installment(row, today) for row in rows]

    open_expenses = [
        i for i in items
        if i.get("bill_type") == "expense"
        and not i.get("paid")
        and not i.get("cancelled")
        and i.get("effective_date")
        and today <= i["effective_date"] <= end
    ]

    def text(i):
        return " ".join([
            _normalized(i.get("project")),
            _normalized(i.get("description")),
            _normalized(i.get("counterparty")),
        ])

    sc_all = [i for i in open_expenses if i.get("is_sc")]
    broad = [
        i for i in open_expenses
        if i.get("is_sc") or any(k in text(i) for k in (
            "blumenau", "sao jose", "eletrobras", "axia", "valmor", "4600001441", "4600002183"
        ))
    ]

    blumenau = [i for i in broad if "blumenau" in text(i) or "4600002183" in text(i)]
    sao_jose = [i for i in broad if "sao jose" in text(i) or "4600001441" in text(i)]
    unmatched = [i for i in broad if i not in blumenau and i not in sao_jose]

    return {
        "generated_at": now.isoformat(),
        "period_start": today.isoformat(),
        "period_end": end.isoformat(),
        "filter_probe": probes,
        "chosen_filter": chosen,
        "api_reported_count": api_count,
        "fetched_rows": len(rows),
        "open_expense_count": len(open_expenses),
        "blumenau_total": sum(float(i.get("amount") or 0) for i in blumenau),
        "sao_jose_total": sum(float(i.get("amount") or 0) for i in sao_jose),
        "unmatched_sc_related_total": sum(float(i.get("amount") or 0) for i in unmatched),
        "blumenau": [_view(i) for i in sorted(blumenau, key=lambda x: (x["effective_date"], -x["amount"]))],
        "sao_jose": [_view(i) for i in sorted(sao_jose, key=lambda x: (x["effective_date"], -x["amount"]))],
        "unmatched_sc_related": [_view(i) for i in sorted(unmatched, key=lambda x: (x["effective_date"], -x["amount"]))],
        "sc_all_total": sum(float(i.get("amount") or 0) for i in sc_all),
        "sc_all": [_view(i) for i in sorted(sc_all, key=lambda x: (x["effective_date"], -x["amount"]))],
    }
