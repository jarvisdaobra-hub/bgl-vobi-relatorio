from datetime import datetime

from reporting import TZ, _normalized, fetch_installments, normalize_installment, vobi_token


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


def build_sc_audit():
    now = datetime.now(TZ)
    today = now.date()
    end = datetime(2026, 9, 30).date()
    token = vobi_token()
    # Query only the target window so the VOBI 10k result cap cannot hide current entries.
    rows, api_count = fetch_installments(token, today, end)
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
            "blumenau", "sao jose", "são josé", "eletrobras", "axia", "valmor", "4600001441", "4600002183"
        ))
    ]

    blumenau = [i for i in broad if "blumenau" in text(i) or "4600002183" in text(i)]
    sao_jose = [i for i in broad if "sao jose" in text(i) or "são josé" in text(i) or "4600001441" in text(i)]
    unmatched = [i for i in broad if i not in blumenau and i not in sao_jose]

    by_project = {}
    for i in broad:
        key = i.get("project") or "Sem obra/projeto"
        by_project[key] = by_project.get(key, 0.0) + float(i.get("amount") or 0)

    return {
        "generated_at": now.isoformat(),
        "period_start": today.isoformat(),
        "period_end": end.isoformat(),
        "api_count": api_count,
        "open_expense_count": len(open_expenses),
        "blumenau_total": sum(float(i.get("amount") or 0) for i in blumenau),
        "sao_jose_total": sum(float(i.get("amount") or 0) for i in sao_jose),
        "unmatched_sc_related_total": sum(float(i.get("amount") or 0) for i in unmatched),
        "blumenau": [_view(i) for i in sorted(blumenau, key=lambda x: (x["effective_date"], -x["amount"]))],
        "sao_jose": [_view(i) for i in sorted(sao_jose, key=lambda x: (x["effective_date"], -x["amount"]))],
        "unmatched_sc_related": [_view(i) for i in sorted(unmatched, key=lambda x: (x["effective_date"], -x["amount"]))],
        "candidate_projects": sorted(
            [{"project": k, "total": v} for k, v in by_project.items()],
            key=lambda x: x["total"],
            reverse=True,
        ),
        "sc_all_total": sum(float(i.get("amount") or 0) for i in sc_all),
        "sc_all": [_view(i) for i in sorted(sc_all, key=lambda x: (x["effective_date"], -x["amount"]))],
    }
