import os
from collections import defaultdict
from datetime import datetime, timedelta

from reporting import (
    TZ,
    _daily_projection,
    _normalized,
    _parse_date,
    fetch_installments,
    fetch_opening_balance,
    normalize_installment,
    vobi_token,
)


def _money_sum(items, bill_type):
    return sum(item["amount"] for item in items if item["bill_type"] == bill_type)


def _window(today, days, events, opening_balance):
    end = today + timedelta(days=days)
    selected = [e for e in events if today <= e["effective_date"] <= end]
    income = _money_sum(selected, "income")
    expense = _money_sum(selected, "expense")
    minimum, minimum_date, closing = _daily_projection(today, end, selected, opening_balance)
    return {
        "days": days,
        "end": end.isoformat(),
        "income": income,
        "expense": expense,
        "net": income - expense,
        "closing_balance": closing,
        "minimum_balance": minimum,
        "minimum_balance_date": minimum_date.isoformat(),
        "required_cash": max(0.0, -minimum),
    }


def _aging(today, events):
    buckets = {
        "1_7": {"count": 0, "value": 0.0},
        "8_15": {"count": 0, "value": 0.0},
        "16_30": {"count": 0, "value": 0.0},
        "31_60": {"count": 0, "value": 0.0},
        "over_60": {"count": 0, "value": 0.0},
    }
    for e in events:
        if e["bill_type"] != "income" or not e["overdue"] or not e["due_date"]:
            continue
        delay = (today - e["due_date"]).days
        if delay <= 7:
            key = "1_7"
        elif delay <= 15:
            key = "8_15"
        elif delay <= 30:
            key = "16_30"
        elif delay <= 60:
            key = "31_60"
        else:
            key = "over_60"
        buckets[key]["count"] += 1
        buckets[key]["value"] += e["amount"]
    return buckets


def _project_summary(events):
    by_project = defaultdict(lambda: {"income": 0.0, "expense": 0.0, "is_sc": False})
    for e in events:
        p = by_project[e["project"]]
        p[e["bill_type"]] += e["amount"]
        p["is_sc"] = p["is_sc"] or e["is_sc"]
    rows = []
    for name, values in by_project.items():
        rows.append({
            "name": name,
            "income": values["income"],
            "expense": values["expense"],
            "net": values["income"] - values["expense"],
            "region": "SC" if values["is_sc"] else "Outros",
        })
    consuming = sorted(rows, key=lambda x: (x["net"], -x["expense"]))[:8]
    generating = sorted(rows, key=lambda x: (x["net"], x["income"]), reverse=True)[:8]
    return consuming, generating


def _flow_class(event):
    project = _normalized(event.get("project"))
    description = _normalized(event.get("description"))
    counterparty = _normalized(event.get("counterparty"))
    text = f"{project} {description} {counterparty}"

    financial_terms = (
        "sicoob",
        "antecip",
        "emprestimo",
        "financiamento",
        "parcelamento",
        "cartao de credito",
        "iof",
        "juros",
        "tarifa bancaria",
        "capital de giro",
    )
    if any(term in text for term in financial_terms):
        return "financeiro"
    if "administrativo" in project or "centro de custo - administrativo" in project:
        return "administrativo"
    return "operacional"


def _flow_breakdown(events):
    buckets = {
        "operacional": {"income": 0.0, "expense": 0.0},
        "administrativo": {"income": 0.0, "expense": 0.0},
        "financeiro": {"income": 0.0, "expense": 0.0},
    }
    for e in events:
        bucket = buckets[_flow_class(e)]
        bucket[e["bill_type"]] += e["amount"]
    for bucket in buckets.values():
        bucket["net"] = bucket["income"] - bucket["expense"]
    return buckets


def _event_view(e):
    return {
        "date": e["effective_date"].isoformat() if e.get("effective_date") else None,
        "due_date": e["due_date"].isoformat() if e.get("due_date") else None,
        "bill_type": e["bill_type"],
        "project": e["project"],
        "counterparty": e["counterparty"],
        "description": e["description"],
        "amount": e["amount"],
        "flow_class": _flow_class(e),
    }


def build_controller_snapshot():
    now = datetime.now(TZ)
    today = now.date()
    horizon_end = today + timedelta(days=45)
    token = vobi_token()
    rows, api_count = fetch_installments(token, today - timedelta(days=365), horizon_end)
    opening_balance, balance_source = fetch_opening_balance(token)

    normalized = [normalize_installment(row, today) for row in rows]
    events = []
    ignored = 0
    unknown = 0
    missing_date = 0
    paid = 0
    cancelled = 0

    for item in normalized:
        if item["paid"]:
            paid += 1
            continue
        if item["cancelled"]:
            cancelled += 1
            continue
        if item["ignored"]:
            ignored += 1
            continue
        if item["bill_type"] not in {"income", "expense"}:
            unknown += 1
            continue
        if not item["effective_date"]:
            missing_date += 1
            continue
        if item["effective_date"] <= horizon_end:
            events.append(item)

    windows = {str(days): _window(today, days, events, opening_balance) for days in (7, 15, 30, 45)}
    minimum_balance, minimum_date, final_balance = _daily_projection(today, horizon_end, events, opening_balance)

    overdue_income = [e for e in events if e["overdue"] and e["bill_type"] == "income"]
    overdue_expense = [e for e in events if e["overdue"] and e["bill_type"] == "expense"]

    rule_days = max(1, int(os.getenv("PAYMENT_RULE_DAYS", "7")))
    discipline = []
    for raw, item in zip(rows, normalized):
        if item["bill_type"] != "expense" or item["paid"] or item["cancelled"] or item["ignored"] or not item["due_date"]:
            continue
        created = _parse_date(raw.get("createdAt") if isinstance(raw, dict) else None)
        if not created:
            continue
        lead = (item["due_date"] - created).days
        discipline.append({
            "supplier": item["counterparty"],
            "project": item["project"],
            "amount": item["amount"],
            "due_date": item["due_date"].isoformat(),
            "created_date": created.isoformat(),
            "lead_days": lead,
            "within_rule": lead >= rule_days,
        })
    outside = [x for x in discipline if not x["within_rule"]]
    inside = [x for x in discipline if x["within_rule"]]

    events_45 = [e for e in events if today <= e["effective_date"] <= horizon_end]
    consuming, generating = _project_summary(events_45)
    missing_project = [e for e in events_45 if e["project"] == "Sem obra/projeto"]
    financial_items = [e for e in events_45 if _flow_class(e) == "financeiro"]

    duplicate_groups = defaultdict(list)
    for e in events:
        if e["bill_type"] != "expense" or not e["due_date"]:
            continue
        key = (e["counterparty"].strip().lower(), round(e["amount"], 2), e["due_date"].isoformat())
        duplicate_groups[key].append(e)
    possible_duplicates = []
    for (supplier, amount, due), group in duplicate_groups.items():
        if len(group) > 1:
            possible_duplicates.append({
                "supplier": supplier,
                "amount_each": amount,
                "due_date": due,
                "count": len(group),
                "total": amount * len(group),
                "projects": sorted({g["project"] for g in group}),
            })
    possible_duplicates.sort(key=lambda x: x["total"], reverse=True)

    top_expenses_30 = sorted(
        [e for e in events if e["bill_type"] == "expense" and today <= e["effective_date"] <= today + timedelta(days=30)],
        key=lambda x: x["amount"],
        reverse=True,
    )[:12]

    return {
        "status": "ok",
        "generated_at": now.isoformat(),
        "source": "VOBI live",
        "opening_balance_source": balance_source,
        "opening_balance": opening_balance,
        "api_count": api_count,
        "projected_rows": len(events),
        "ignored_rows": ignored,
        "unknown_type_rows": unknown,
        "missing_date_rows": missing_date,
        "paid_rows": paid,
        "cancelled_rows": cancelled,
        "windows": windows,
        "overall_45d": {
            "minimum_balance": minimum_balance,
            "minimum_balance_date": minimum_date.isoformat(),
            "required_cash": max(0.0, -minimum_balance),
            "final_balance_45d": final_balance,
        },
        "flow_45d": _flow_breakdown(events_45),
        "financial_items_45d": [_event_view(e) for e in sorted(financial_items, key=lambda x: (x["effective_date"], -x["amount"]))],
        "overdue": {
            "income_count": len(overdue_income),
            "income_value": _money_sum(overdue_income, "income"),
            "expense_count": len(overdue_expense),
            "expense_value": _money_sum(overdue_expense, "expense"),
            "aging_income": _aging(today, events),
        },
        "payment_rule": {
            "rule_days": rule_days,
            "basis": "open expenses with createdAt and dueDate",
            "analyzed": len(discipline),
            "within_rule": len(inside),
            "outside_rule": len(outside),
            "compliance_pct": (100.0 * len(inside) / len(discipline)) if discipline else None,
            "outside_value": sum(x["amount"] for x in outside),
            "largest_deviations": sorted(outside, key=lambda x: x["amount"], reverse=True)[:10],
        },
        "projects_45d": {
            "top_consuming": consuming,
            "top_generating": generating,
            "without_project_count": len(missing_project),
            "without_project_value": sum(e["amount"] for e in missing_project if e["bill_type"] == "expense"),
        },
        "possible_duplicates": possible_duplicates[:10],
        "top_expenses_30d": [
            {
                "date": e["effective_date"].isoformat(),
                "project": e["project"],
                "supplier": e["counterparty"],
                "description": e["description"],
                "amount": e["amount"],
                "flow_class": _flow_class(e),
            }
            for e in top_expenses_30
        ],
        "not_available_from_current_vobi_snapshot": [
            "bank statement balance for independent reconciliation against VOBI currentBalance",
            "commitments not yet entered in VOBI",
            "cost to complete by project",
            "original/current/final projected margin by project",
            "executed but not measured unless separately registered",
            "measured but not invoiced unless separately registered",
            "confidence class of each future receipt",
            "responsible person for each financial entry when not returned by endpoint",
            "operational reserve policy",
        ],
    }
