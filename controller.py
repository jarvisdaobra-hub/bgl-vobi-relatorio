import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

from reporting import (
    TZ,
    _daily_projection,
    _normalized,
    _parse_date,
    fetch_opening_balance,
    normalize_installment,
    vobi_get,
    vobi_token,
)


def _fetch_open_installments(token):
    """Fetch all open VOBI installments.

    VOBI's unfiltered financial/installments endpoint caps count/offset at 10,000
    and ignores the date filters previously used by this integration. The
    idInstallmentStatus filter is honored and keeps the result set below that
    cap. Status 1 is the open/pending status observed in VOBI.
    """
    limit = 500
    offset = 0
    rows = []
    api_count = None

    while True:
        payload = vobi_get(
            "financial/installments",
            token,
            params={
                "limit": limit,
                "offset": offset,
                "where[idInstallmentStatus]": 1,
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
            raise RuntimeError("VOBI open-installment result unexpectedly reached 10,000-row offset cap")

    return rows, api_count if api_count is not None else len(rows)


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
        "boleto sicoob",
        "antecip",
        "emprestimo",
        "financiamento",
        "amortizacao",
        "parcelamento facil",
        "capital de giro",
        "iof",
        "juros",
        "tarifa bancaria",
        "consorcio",
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


def _financing_quality(events):
    """Separate productive receivables financing from debt service/rollover.

    An anticipation is not treated as a negative signal by itself. It becomes a
    rollover risk only when the data can prove that a new financial operation is
    being used to settle prior debt without the underlying operating cycle
    replenishing cash. VOBI currently does not link each financing item to the
    NF/measurement and the costs that generated that receivable, so the snapshot
    reports the evidence available and leaves cycle coverage as not calculable.
    """
    anticipation_terms = ("antecip", "nf ")
    debt_terms = (
        "boleto sicoob", "emprestimo", "financiamento", "amortizacao",
        "capital de giro", "parcelamento facil", "consorcio", "iof",
        "juros", "tarifa bancaria",
    )
    anticipation_expense = 0.0
    other_debt_service = 0.0
    anticipation_items = []
    debt_items = []

    for e in events:
        if e.get("bill_type") != "expense":
            continue
        text = _normalized(f"{e.get('counterparty') or ''} {e.get('description') or ''}")
        if "antecip" in text:
            anticipation_expense += e["amount"]
            anticipation_items.append(e)
        elif any(term in text for term in debt_terms):
            other_debt_service += e["amount"]
            debt_items.append(e)

    operating_income = sum(
        e["amount"] for e in events
        if e.get("bill_type") == "income" and _flow_class(e) == "operacional"
    )
    operating_expense = sum(
        e["amount"] for e in events
        if e.get("bill_type") == "expense" and _flow_class(e) == "operacional"
    )

    return {
        "policy": (
            "Antecipacao de NF nao e classificada automaticamente como problema. "
            "E financiamento produtivo quando a NF/medicao recompõe o custo ja "
            "desembolsado, quita principal+custo financeiro e preserva a margem. "
            "Risco de bola de neve existe quando nova divida paga divida anterior "
            "sem recomposicao suficiente pelo ciclo operacional."
        ),
        "operating_income": operating_income,
        "operating_expense": operating_expense,
        "operating_net_before_financing": operating_income - operating_expense,
        "anticipation_debt_service": anticipation_expense,
        "other_debt_service": other_debt_service,
        "total_identified_debt_service": anticipation_expense + other_debt_service,
        "anticipation_items": [_event_view(e) for e in sorted(anticipation_items, key=lambda x: (x["effective_date"], -x["amount"]))],
        "other_debt_items": [_event_view(e) for e in sorted(debt_items, key=lambda x: (x["effective_date"], -x["amount"]))],
        "cycle_coverage_status": "NAO CALCULAVEL COM SEGURANCA",
        "cycle_coverage_formula": (
            "NF liquida - custo desembolsado da etapa - principal da antecipacao "
            "- juros/custos financeiros - compromissos para concluir a receita"
        ),
        "missing_for_cycle_coverage": [
            "vinculo entre cada NF/medicao e a antecipacao correspondente",
            "custo desembolsado atribuivel a cada NF/medicao",
            "juros/taxas efetivos por operacao quando nao lancados como valor",
            "compromissos ainda necessarios para concluir a receita da medicao",
            "margem prevista da etapa/obra",
        ],
        "rollover_risk": "NAO INFERIR SEM VINCULO ENTRE DIVIDA, NF/MEDICAO E CUSTOS",
    }


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


def _sao_jose_year_end(normalized, today):
    year_end = today.replace(month=12, day=31)
    items = []
    missing_date = []
    for item in normalized:
        if item.get("paid") or item.get("cancelled"):
            continue
        if item.get("bill_type") not in {"income", "expense"}:
            continue
        if "sao jose" not in _normalized(item.get("project")):
            continue
        due = item.get("due_date")
        if not due:
            missing_date.append(item)
            continue
        if due <= year_end:
            items.append(item)

    incomes = [x for x in items if x["bill_type"] == "income"]
    expenses = [x for x in items if x["bill_type"] == "expense"]
    included_expenses = [x for x in expenses if not x.get("ignored")]
    excluded_expenses = [x for x in expenses if x.get("ignored")]

    def excluded_class(item):
        text = _normalized(f"{item.get('counterparty') or ''} {item.get('description') or ''}")
        if "valmor" in text:
            return "valmor"
        tax_terms = ("receita federal", "darf", "irpj", "csll", "cofins", "pis", "inss", "fgts", "iss", "tribut")
        if any(term in text for term in tax_terms):
            return "tributos"
        return "outros_excluidos"

    excluded_by_class = {"valmor": 0.0, "tributos": 0.0, "outros_excluidos": 0.0}
    for item in excluded_expenses:
        excluded_by_class[excluded_class(item)] += item["amount"]

    monthly = {}
    for item in items:
        month = item["due_date"].strftime("%Y-%m")
        bucket = monthly.setdefault(month, {
            "income": 0.0,
            "expense_all": 0.0,
            "expense_included": 0.0,
            "expense_excluded": 0.0,
        })
        if item["bill_type"] == "income":
            bucket["income"] += item["amount"]
        else:
            bucket["expense_all"] += item["amount"]
            if item.get("ignored"):
                bucket["expense_excluded"] += item["amount"]
            else:
                bucket["expense_included"] += item["amount"]

    result = {
        "through": year_end.isoformat(),
        "open_income_total": _money_sum(incomes, "income"),
        "open_expense_total_all": _money_sum(expenses, "expense"),
        "open_expense_controller_included": _money_sum(included_expenses, "expense"),
        "open_expense_controller_excluded": _money_sum(excluded_expenses, "expense"),
        "excluded_by_class": excluded_by_class,
        "net_open_all": _money_sum(incomes, "income") - _money_sum(expenses, "expense"),
        "net_open_controller": _money_sum(incomes, "income") - _money_sum(included_expenses, "expense"),
        "monthly": monthly,
        "top_incomes": [_event_view(x) for x in sorted(incomes, key=lambda x: x["amount"], reverse=True)[:20]],
        "top_expenses_all": [_event_view(x) for x in sorted(expenses, key=lambda x: x["amount"], reverse=True)[:30]],
        "excluded_expenses": [_event_view(x) for x in sorted(excluded_expenses, key=lambda x: x["amount"], reverse=True)[:30]],
        "missing_date_count": len(missing_date),
        "missing_date_value": sum(x["amount"] for x in missing_date),
    }
    print("SAO_JOSE_YEAR_END " + json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return result


def build_controller_snapshot():
    now = datetime.now(TZ)
    today = now.date()
    horizon_end = today + timedelta(days=45)
    token = vobi_token()
    rows, api_count = _fetch_open_installments(token)
    opening_balance, balance_source = fetch_opening_balance(token)

    normalized = [normalize_installment(row, today) for row in rows]
    sao_jose_year_end = _sao_jose_year_end(normalized, today)
    events = []
    ignored_items = []
    unknown = 0
    missing_date = 0
    paid = 0
    cancelled = 0

    for item in normalized:
        if item.get("due_date"):
            item["effective_date"] = max(today, item["due_date"])
            item["lead_days_adjusted"] = False

        if item["paid"]:
            paid += 1
            continue
        if item["cancelled"]:
            cancelled += 1
            continue
        if item["ignored"]:
            ignored_items.append(item)
            continue
        if item["bill_type"] not in {"income", "expense"}:
            unknown += 1
            continue
        if not item["effective_date"]:
            missing_date += 1
            continue
        if item["effective_date"] <= horizon_end:
            events.append(item)

    windows = {str(days): _window(today, days, events, opening_balance) for days in (7, 9, 15, 19, 29, 30, 39, 45)}
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
    ignored_45 = [
        e for e in ignored_items
        if e.get("effective_date") and today <= e["effective_date"] <= horizon_end
    ]
    consuming, generating = _project_summary(events_45)
    missing_project = [e for e in events_45 if e["project"] == "Sem obra/projeto"]
    financial_items = [e for e in events_45 if _flow_class(e) == "financeiro"]
    sep16_items = [e for e in events_45 if e.get("due_date") and e["due_date"].isoformat() == "2026-09-16"]
    blumenau_items = [e for e in events_45 if "blumenau" in _normalized(e.get("project"))]
    sao_jose_items = [e for e in events_45 if "sao jose" in _normalized(e.get("project"))]

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

    # Caixa Zero: reset from today forward. Historical overdue rows are not
    # rolled into today, and agreed deferred suppliers stay outside the cash plan.
    cash_zero_events = []
    for e in events_45:
        due = e.get("due_date")
        if not due or due < today:
            continue
        text = _normalized(f"{e.get('counterparty') or ''} {e.get('description') or ''}")
        if "remabombas" in text or "valmor" in text:
            continue
        # Caixa Zero rule confirmed on 2026-10-06:
        # every expense due today has already been paid and must not remain
        # in the forward cash projection.
        if e.get("bill_type") == "expense" and e.get("due_date") == today:
            continue

        # Manual receivable correction confirmed by Bruno on 2026-10-06:
        # São José forecast of R$ 186,131.72 due 2026-10-16 is not valid.
        # Correct amount is R$ 62,250.00 due 2026-11-06.
        if (
            e.get("bill_type") == "income"
            and "sao jose" in _normalized(e.get("project"))
            and round(float(e.get("amount") or 0), 2) == 186131.72
            and e.get("due_date") and e["due_date"].isoformat() == "2026-10-16"
        ):
            continue
        cash_zero_events.append(e)

    cash_zero_events.append({
        "effective_date": datetime(2026, 11, 6).date(),
        "due_date": datetime(2026, 11, 6).date(),
        "bill_type": "income",
        "project": "Execução - Eletrobrás São José/SC",
        "counterparty": "Eletrobrás CGT Eletrosul",
        "description": "Caixa Zero - correção manual recebimento São José",
        "amount": 62250.0,
        "is_sc": True,
        "overdue": False,
    })

    # Exact 15-day forward view starts tomorrow because today's opening
    # balance is the real post-payment bank balance supplied by the user.
    cash_zero_15d_start = today + timedelta(days=1)
    cash_zero_15d_end = today + timedelta(days=15)
    cash_zero_15d_events = [
        e for e in cash_zero_events
        if e.get("due_date") and cash_zero_15d_start <= e["due_date"] <= cash_zero_15d_end
    ]
    cash_zero_15d_income = sum(e["amount"] for e in cash_zero_15d_events if e["bill_type"] == "income")
    cash_zero_15d_expense = sum(e["amount"] for e in cash_zero_15d_events if e["bill_type"] == "expense")
    cash_zero_15d = {
        "start": cash_zero_15d_start.isoformat(),
        "end": cash_zero_15d_end.isoformat(),
        "income": cash_zero_15d_income,
        "expense": cash_zero_15d_expense,
        "net": cash_zero_15d_income - cash_zero_15d_expense,
        "daily_net": [
            {
                "date": d.isoformat(),
                "income": sum(e["amount"] for e in cash_zero_15d_events if e["due_date"] == d and e["bill_type"] == "income"),
                "expense": sum(e["amount"] for e in cash_zero_15d_events if e["due_date"] == d and e["bill_type"] == "expense"),
            }
            for d in [cash_zero_15d_start + timedelta(days=i) for i in range((cash_zero_15d_end - cash_zero_15d_start).days + 1)]
        ],
        "top_incomes": [_event_view(e) for e in sorted(
            [x for x in cash_zero_15d_events if x["bill_type"] == "income"],
            key=lambda x: x["amount"], reverse=True
        )[:12]],
        "top_expenses": [_event_view(e) for e in sorted(
            [x for x in cash_zero_15d_events if x["bill_type"] == "expense"],
            key=lambda x: x["amount"], reverse=True
        )[:16]],
    }

    cash_zero_blocks_10d = []
    for start_offset in range(0, 45, 10):
        block_start = today + timedelta(days=start_offset)
        block_end = min(horizon_end, block_start + timedelta(days=9))
        selected = [
            e for e in cash_zero_events
            if block_start <= e["due_date"] <= block_end
        ]
        incomes = sorted(
            [e for e in selected if e["bill_type"] == "income"],
            key=lambda x: x["amount"],
            reverse=True,
        )
        expenses = sorted(
            [e for e in selected if e["bill_type"] == "expense"],
            key=lambda x: x["amount"],
            reverse=True,
        )
        income = sum(e["amount"] for e in incomes)
        expense = sum(e["amount"] for e in expenses)
        cash_zero_blocks_10d.append({
            "start": block_start.isoformat(),
            "end": block_end.isoformat(),
            "income": income,
            "expense": expense,
            "net": income - expense,
            "top_incomes": [_event_view(e) for e in incomes[:8]],
            "top_expenses": [_event_view(e) for e in expenses[:12]],
        })

    return {
        "status": "ok",
        "generated_at": now.isoformat(),
        "source": "VOBI live - open installments (status 1)",
        "opening_balance_source": balance_source,
        "opening_balance": opening_balance,
        "api_count": api_count,
        "fetched_open_rows": len(rows),
        "projected_rows": len(events),
        "ignored_rows": len(ignored_items),
        "ignored_value_45d": sum(e["amount"] for e in ignored_45 if e.get("bill_type") == "expense"),
        "unknown_type_rows": unknown,
        "missing_date_rows": missing_date,
        "paid_rows": paid,
        "cancelled_rows": cancelled,
        "sao_jose_year_end": sao_jose_year_end,
        "windows": windows,
        "cash_zero_tomorrow_expenses": [
            _event_view(e) for e in sorted(
                [
                    x for x in cash_zero_events
                    if x.get("bill_type") == "expense"
                    and x.get("due_date") == today + timedelta(days=1)
                ],
                key=lambda x: x["amount"],
                reverse=True,
            )
        ],
        "cash_zero_15d": cash_zero_15d,
        "cash_zero_blocks_10d": cash_zero_blocks_10d,
        "overall_45d": {
            "minimum_balance": minimum_balance,
            "minimum_balance_date": minimum_date.isoformat(),
            "required_cash": max(0.0, -minimum_balance),
            "final_balance_45d": final_balance,
        },
        "flow_45d": _flow_breakdown(events_45),
        "financing_quality_45d": _financing_quality(events_45),
        "financial_items_45d": [_event_view(e) for e in sorted(financial_items, key=lambda x: (x["effective_date"], -x["amount"]))],
        "sep16_items": [_event_view(e) for e in sorted(sep16_items, key=lambda x: -x["amount"])],
        "blumenau_items_45d": [_event_view(e) for e in sorted(blumenau_items, key=lambda x: (x["effective_date"], x["bill_type"], -x["amount"]))],
        "sao_jose_items_45d": [_event_view(e) for e in sorted(sao_jose_items, key=lambda x: (x["effective_date"], x["bill_type"], -x["amount"]))],
        "excluded_items_45d": [_event_view(e) for e in sorted(ignored_45, key=lambda x: (x["effective_date"], -x["amount"]))],
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
            "link between each NF/measurement, its financed costs and its anticipation/loan",
            "cycle coverage: whether each NF repays spent cost + financing principal/cost + preserves margin",
        ],
    }
