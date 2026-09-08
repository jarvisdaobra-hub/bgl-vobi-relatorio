import calendar
import html
import json
import os
import re
import smtplib
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests


TZ = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/Sao_Paulo"))
PAID_STATUSES = {2, 3, 4, 7, 8, 9, 10, 11}
CANCELLED_STATUSES = {6, 12}
DEFAULT_IGNORE_TERMS = "valmor,impostos,tributos,darf,fgts,inss,iss,irpj,csll,cofins,pis"
DEFAULT_SC_KEYWORDS = "blumenau,sao jose,são josé,santa catarina"


def vobi_base_url():
    return os.getenv("VOBI_API_BASE_URL", "https://api.vobi.com.br/v2").rstrip("/")


def vobi_token():
    uuid = os.environ["VOBI_UUID"]
    secret = os.environ["VOBI_CLIENT_SECRET"]
    response = requests.post(
        f"{vobi_base_url()}/auth/token",
        auth=(uuid, secret),
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("jwt")
    if not token:
        raise RuntimeError("Vobi retornou resposta sem jwt")
    return token


def vobi_get(path, token, params=None):
    response = requests.get(
        f"{vobi_base_url()}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _safe_float(value, default=0.0):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _normalized(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.lower()).strip()


def _get_nested(obj, *paths):
    for path in paths:
        current = obj
        ok = True
        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                ok = False
                break
            current = current[key]
        if ok and current not in (None, ""):
            return current
    return None


def fetch_installments(token, initial_date, end_date):
    limit = max(1, min(int(os.getenv("VOBI_PAGE_SIZE", "500")), 1000))
    max_rows = max(limit, int(os.getenv("VOBI_MAX_ROWS", "20000")))
    offset = 0
    rows = []
    api_count = None

    while len(rows) < max_rows:
        payload = vobi_get(
            "financial/installments",
            token,
            params={
                "limit": limit,
                "offset": offset,
                "where[initialDate]": initial_date.isoformat(),
                "where[endDate]": end_date.isoformat(),
            },
        )
        batch = payload.get("rows", []) if isinstance(payload, dict) else []
        api_count = _safe_int(payload.get("count"), len(batch)) if isinstance(payload, dict) else len(batch)
        rows.extend(batch)

        if not batch or len(rows) >= api_count or len(batch) < limit:
            break
        offset += len(batch)

    return rows[:max_rows], api_count if api_count is not None else len(rows)


def _balance_from_item(item):
    if not isinstance(item, dict):
        return None
    for key in (
        "currentBalance",
        "availableBalance",
        "totalBalance",
        "balance",
        "current",
    ):
        if key in item and isinstance(item[key], (int, float)):
            return float(item[key])
    return None


def _extract_balance(payload):
    if isinstance(payload, dict):
        for key in ("totalBalance", "currentBalance", "availableBalance", "balance"):
            if key in payload and isinstance(payload[key], (int, float)):
                return float(payload[key]), f"vobi:{key}"

        for collection_key in ("bankAccounts", "accounts", "rows", "items", "data"):
            collection = payload.get(collection_key)
            if isinstance(collection, list) and collection:
                balances = [_balance_from_item(item) for item in collection]
                balances = [value for value in balances if value is not None]
                if balances:
                    return sum(balances), f"vobi:{collection_key}"

        if "total" in payload and isinstance(payload["total"], (int, float)):
            return float(payload["total"]), "vobi:total"

    if isinstance(payload, list) and payload:
        balances = [_balance_from_item(item) for item in payload]
        balances = [value for value in balances if value is not None]
        if balances:
            return sum(balances), "vobi:list"

    return None, None


def fetch_opening_balance(token):
    try:
        payload = vobi_get("financial/balanceByBankAccount", token)
        balance, source = _extract_balance(payload)
        if balance is not None:
            return balance, source
    except requests.HTTPError:
        pass

    fallback = _safe_float(os.getenv("OPENING_BALANCE"), 0.0)
    return fallback, "env:OPENING_BALANCE" if os.getenv("OPENING_BALANCE") else "fallback:0"


def _detect_bill_type(row):
    raw = _get_nested(row, "billType", "payment.billType", "bill.billType")
    value = _normalized(raw)
    if value in {"income", "receita", "receber"}:
        return "income"
    if value in {"expense", "despesa", "pagar"}:
        return "expense"

    supplier = _get_nested(row, "idSupplier", "supplierName", "supplier.name")
    customer = _get_nested(row, "idCompanyCustomer", "customerName", "customer.name")
    if supplier and not customer:
        return "expense"
    if customer and not supplier:
        return "income"
    return None


def _ignore_terms():
    raw = os.getenv("IGNORE_FINANCIAL_TERMS", DEFAULT_IGNORE_TERMS)
    return [_normalized(term) for term in raw.split(",") if term.strip()]


def _is_ignored_expense(row, bill_type):
    if bill_type != "expense":
        return False
    parts = [
        _get_nested(row, "description", "paymentDescription", "payment.description"),
        _get_nested(row, "supplierName", "supplier.name"),
        _get_nested(row, "financialCategoryName", "categoryName", "financialCategory.name"),
        _get_nested(row, "refurbishName", "refurbish.name"),
    ]
    text = _normalized(" ".join(str(part or "") for part in parts))
    tokens = set(re.findall(r"[a-z0-9]+", text))
    for term in _ignore_terms():
        if not term:
            continue
        if " " in term and term in text:
            return True
        if term in tokens:
            return True
    return False


def _project_name(row):
    return str(
        _get_nested(row, "refurbishName", "refurbish.name", "projectName", "project.name")
        or "Sem obra/projeto"
    ).strip()


def _counterparty(row, bill_type):
    if bill_type == "expense":
        value = _get_nested(row, "supplierName", "supplier.name")
    else:
        value = _get_nested(row, "customerName", "customer.name")
    return str(value or "Não informado").strip()


def _issue_date(row):
    value = _get_nested(
        row,
        "invoiceIssueDate",
        "issueDate",
        "emissionDate",
        "invoice.issueDate",
        "invoice.emissionDate",
    )
    return _parse_date(value)


def _is_sc_project(project):
    project_norm = _normalized(project)
    keywords = [
        _normalized(item)
        for item in os.getenv("SC_PROJECT_KEYWORDS", DEFAULT_SC_KEYWORDS).split(",")
        if item.strip()
    ]
    return any(keyword and keyword in project_norm for keyword in keywords)


def normalize_installment(row, today):
    status = _safe_int(_get_nested(row, "idInstallmentStatus", "installmentStatus.id"))
    bill_type = _detect_bill_type(row)
    due_date = _parse_date(_get_nested(row, "dueDate", "date", "payment.dueDate"))
    amount = _safe_float(_get_nested(row, "price", "originalValue", "value", "amount"), 0.0)
    amount = abs(amount)
    project = _project_name(row)

    result = {
        "id": str(_get_nested(row, "id", "uuid") or ""),
        "status": status,
        "bill_type": bill_type,
        "due_date": due_date,
        "effective_date": due_date,
        "amount": amount,
        "project": project,
        "counterparty": _counterparty(row, bill_type),
        "description": str(_get_nested(row, "description", "paymentDescription", "payment.description") or "").strip(),
        "is_sc": _is_sc_project(project),
        "paid": status in PAID_STATUSES,
        "cancelled": status in CANCELLED_STATUSES,
        "ignored": False,
        "overdue": bool(due_date and due_date < today),
        "lead_days_adjusted": False,
    }

    result["ignored"] = _is_ignored_expense(row, bill_type)

    if bill_type == "expense" and due_date:
        issue = _issue_date(row)
        min_days = max(0, int(os.getenv("MIN_PAYMENT_LEAD_DAYS", "15")))
        if issue and min_days:
            allowed = issue + timedelta(days=min_days)
            if result["effective_date"] < allowed:
                result["effective_date"] = allowed
                result["lead_days_adjusted"] = True

    if result["effective_date"] and result["effective_date"] < today:
        result["effective_date"] = today

    return result


def _add_months(day, months):
    month_index = day.year * 12 + (day.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def _weekly_periods(today, count=8):
    periods = []
    start = today
    first_end = today + timedelta(days=(6 - today.weekday()))
    periods.append((start, first_end))
    start = first_end + timedelta(days=1)
    while len(periods) < count:
        end = start + timedelta(days=6)
        periods.append((start, end))
        start = end + timedelta(days=1)
    return periods


def _fortnight_periods(today, months=6):
    final = _add_months(today, months) - timedelta(days=1)
    periods = []
    cursor = today
    while cursor <= final:
        if cursor.day <= 15:
            end = min(date(cursor.year, cursor.month, 15), final)
        else:
            end = min(_month_end(cursor.year, cursor.month), final)
        periods.append((cursor, end))
        cursor = end + timedelta(days=1)
    return periods


def _monthly_periods(today, months=12):
    periods = []
    for offset in range(months):
        base = _add_months(date(today.year, today.month, 1), offset)
        start = today if offset == 0 else base
        end = _month_end(base.year, base.month)
        periods.append((start, end))
    return periods


def _aggregate_periods(periods, events, opening_balance):
    result = []
    running = opening_balance
    for start, end in periods:
        selected = [event for event in events if start <= event["effective_date"] <= end]
        income = sum(event["amount"] for event in selected if event["bill_type"] == "income")
        expense = sum(event["amount"] for event in selected if event["bill_type"] == "expense")
        net = income - expense
        running += net
        result.append(
            {
                "start": start,
                "end": end,
                "income": income,
                "expense": expense,
                "net": net,
                "closing": running,
            }
        )
    return result


def _daily_projection(today, horizon_end, events, opening_balance):
    by_date = defaultdict(float)
    for event in events:
        sign = 1.0 if event["bill_type"] == "income" else -1.0
        by_date[event["effective_date"]] += sign * event["amount"]

    balance = opening_balance
    min_balance = opening_balance
    min_date = today
    cursor = today
    while cursor <= horizon_end:
        balance += by_date.get(cursor, 0.0)
        if balance < min_balance:
            min_balance = balance
            min_date = cursor
        cursor += timedelta(days=1)
    return min_balance, min_date, balance


def _safe_meta(meta):
    return {
        key: value.isoformat() if isinstance(value, date) else value
        for key, value in meta.items()
    }


def build_report_data():
    now = datetime.now(TZ)
    today = now.date()
    horizon_end = _add_months(today, 12) - timedelta(days=1)
    token = vobi_token()
    rows, api_count = fetch_installments(token, today - timedelta(days=365), horizon_end)
    opening_balance, balance_source = fetch_opening_balance(token)

    normalized = [normalize_installment(row, today) for row in rows]
    meta = {
        "generated_at": now.isoformat(),
        "api_count": api_count,
        "fetched_rows": len(rows),
        "paid_rows": 0,
        "cancelled_rows": 0,
        "ignored_rows": 0,
        "unknown_type_rows": 0,
        "missing_date_rows": 0,
        "lead_days_adjusted_rows": 0,
        "projected_rows": 0,
        "opening_balance_source": balance_source,
    }

    events = []
    for item in normalized:
        if item["paid"]:
            meta["paid_rows"] += 1
            continue
        if item["cancelled"]:
            meta["cancelled_rows"] += 1
            continue
        if item["ignored"]:
            meta["ignored_rows"] += 1
            continue
        if item["bill_type"] not in {"income", "expense"}:
            meta["unknown_type_rows"] += 1
            continue
        if not item["effective_date"]:
            meta["missing_date_rows"] += 1
            continue
        if item["effective_date"] > horizon_end:
            continue
        if item["lead_days_adjusted"]:
            meta["lead_days_adjusted_rows"] += 1
        events.append(item)

    meta["projected_rows"] = len(events)

    min_balance, min_date, final_balance = _daily_projection(today, horizon_end, events, opening_balance)
    required_cash = max(0.0, -min_balance)

    d30 = today + timedelta(days=30)
    d60 = today + timedelta(days=60)
    events_30 = [event for event in events if event["effective_date"] <= d30]
    events_60 = [event for event in events if event["effective_date"] <= d60]

    income_30 = sum(event["amount"] for event in events_30 if event["bill_type"] == "income")
    expense_30 = sum(event["amount"] for event in events_30 if event["bill_type"] == "expense")

    overdue_income = sum(event["amount"] for event in events if event["overdue"] and event["bill_type"] == "income")
    overdue_expense = sum(event["amount"] for event in events if event["overdue"] and event["bill_type"] == "expense")
    overdue_income_count = sum(1 for event in events if event["overdue"] and event["bill_type"] == "income")
    overdue_expense_count = sum(1 for event in events if event["overdue"] and event["bill_type"] == "expense")

    projects = defaultdict(lambda: {"income": 0.0, "expense": 0.0, "sc": False})
    regions = {
        "Santa Catarina": {"income": 0.0, "expense": 0.0},
        "Demais": {"income": 0.0, "expense": 0.0},
    }
    for event in events_60:
        project = projects[event["project"]]
        project[event["bill_type"]] += event["amount"]
        project["sc"] = project["sc"] or event["is_sc"]
        region = "Santa Catarina" if event["is_sc"] else "Demais"
        regions[region][event["bill_type"]] += event["amount"]

    project_rows = []
    for name, values in projects.items():
        net = values["income"] - values["expense"]
        project_rows.append(
            {
                "name": name,
                "income": values["income"],
                "expense": values["expense"],
                "net": net,
                "region": "SC" if values["sc"] else "Outros",
            }
        )
    project_rows.sort(key=lambda item: (item["net"], -item["expense"]))

    top_expenses = sorted(
        [event for event in events_30 if event["bill_type"] == "expense"],
        key=lambda item: item["amount"],
        reverse=True,
    )[:12]

    meta.update(
        {
            "opening_balance": opening_balance,
            "minimum_balance": min_balance,
            "minimum_balance_date": min_date,
            "required_cash": required_cash,
            "final_balance_12m": final_balance,
            "income_30": income_30,
            "expense_30": expense_30,
            "overdue_income": overdue_income,
            "overdue_expense": overdue_expense,
            "overdue_income_count": overdue_income_count,
            "overdue_expense_count": overdue_expense_count,
        }
    )

    return {
        "today": today,
        "horizon_end": horizon_end,
        "meta": meta,
        "weekly": _aggregate_periods(_weekly_periods(today), events, opening_balance),
        "fortnightly": _aggregate_periods(_fortnight_periods(today), events, opening_balance),
        "monthly": _aggregate_periods(_monthly_periods(today), events, opening_balance),
        "projects": project_rows[:15],
        "regions": regions,
        "top_expenses": top_expenses,
    }


def brl(value):
    value = _safe_float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sign}R$ {formatted}"


def _fmt_date(value):
    return value.strftime("%d/%m/%Y") if value else "-"


def _period_label(start, end):
    if start == end:
        return _fmt_date(start)
    if start.year == end.year:
        return f"{start.strftime('%d/%m')}–{end.strftime('%d/%m/%Y')}"
    return f"{_fmt_date(start)}–{_fmt_date(end)}"


def _table_rows(periods):
    rows = []
    for item in periods:
        closing_class = "negative" if item["closing"] < 0 else ""
        rows.append(
            "<tr>"
            f"<td>{html.escape(_period_label(item['start'], item['end']))}</td>"
            f"<td class='num'>{brl(item['income'])}</td>"
            f"<td class='num'>{brl(item['expense'])}</td>"
            f"<td class='num'>{brl(item['net'])}</td>"
            f"<td class='num {closing_class}'>{brl(item['closing'])}</td>"
            "</tr>"
        )
    return "".join(rows)


def render_report_html(data):
    meta = data["meta"]
    warning = ""
    if meta["required_cash"] > 0:
        warning = (
            "<div class='alert danger'><strong>Atenção:</strong> projeção entra no negativo. "
            f"Necessidade mínima estimada: <strong>{brl(meta['required_cash'])}</strong> "
            f"em {_fmt_date(meta['minimum_balance_date'])}.</div>"
        )
    else:
        warning = (
            "<div class='alert ok'><strong>Caixa projetado positivo:</strong> "
            f"menor saldo de {brl(meta['minimum_balance'])} em {_fmt_date(meta['minimum_balance_date'])}.</div>"
        )

    overdue = ""
    if meta["overdue_income_count"] or meta["overdue_expense_count"]:
        overdue = (
            "<div class='alert warn'>"
            f"Em aberto vencido: {meta['overdue_income_count']} recebimentos ({brl(meta['overdue_income'])}) e "
            f"{meta['overdue_expense_count']} pagamentos ({brl(meta['overdue_expense'])}). "
            "Na projeção, os vencidos foram trazidos para hoje."
            "</div>"
        )

    project_html = "".join(
        "<tr>"
        f"<td>{html.escape(item['name'])}</td>"
        f"<td>{item['region']}</td>"
        f"<td class='num'>{brl(item['income'])}</td>"
        f"<td class='num'>{brl(item['expense'])}</td>"
        f"<td class='num {'negative' if item['net'] < 0 else ''}'>{brl(item['net'])}</td>"
        "</tr>"
        for item in data["projects"]
    ) or "<tr><td colspan='5'>Sem movimentação projetada nos próximos 60 dias.</td></tr>"

    expense_html = "".join(
        "<tr>"
        f"<td>{_fmt_date(item['effective_date'])}</td>"
        f"<td>{html.escape(item['project'])}</td>"
        f"<td>{html.escape(item['counterparty'])}</td>"
        f"<td>{html.escape(item['description'] or '-')}</td>"
        f"<td class='num'>{brl(item['amount'])}</td>"
        "</tr>"
        for item in data["top_expenses"]
    ) or "<tr><td colspan='5'>Sem pagamentos projetados nos próximos 30 dias.</td></tr>"

    region_rows = []
    for name in ("Santa Catarina", "Demais"):
        values = data["regions"][name]
        net = values["income"] - values["expense"]
        region_rows.append(
            "<tr>"
            f"<td>{name}</td>"
            f"<td class='num'>{brl(values['income'])}</td>"
            f"<td class='num'>{brl(values['expense'])}</td>"
            f"<td class='num {'negative' if net < 0 else ''}'>{brl(net)}</td>"
            "</tr>"
        )

    generated = datetime.fromisoformat(meta["generated_at"]).astimezone(TZ).strftime("%d/%m/%Y %H:%M")

    return f"""<!doctype html>
<html lang='pt-BR'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>BGL | Fluxo de Caixa VOBI</title>
<style>
body{{font-family:Arial,Helvetica,sans-serif;background:#f4f6f8;color:#1f2937;margin:0;padding:24px}}
.wrap{{max-width:1180px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:17px;margin:26px 0 10px}}
.sub{{color:#6b7280;font-size:13px;margin-bottom:18px}}
.cards{{display:flex;flex-wrap:wrap;gap:10px}}
.card{{background:white;border:1px solid #e5e7eb;border-radius:9px;padding:13px 15px;min-width:180px;flex:1}}
.card small{{display:block;color:#6b7280;margin-bottom:6px}} .card strong{{font-size:20px}}
.alert{{padding:12px 14px;border-radius:8px;margin:12px 0;background:white;border:1px solid #e5e7eb}}
.danger{{border-left:5px solid #b91c1c}} .warn{{border-left:5px solid #b45309}} .ok{{border-left:5px solid #15803d}}
table{{width:100%;border-collapse:collapse;background:white;border:1px solid #e5e7eb;font-size:13px}}
th,td{{padding:9px 10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
th{{background:#f9fafb;color:#374151}} .num{{text-align:right;white-space:nowrap}} .negative{{color:#b91c1c;font-weight:700}}
.note{{font-size:12px;color:#6b7280;line-height:1.5;margin-top:18px}}
@media(max-width:700px){{body{{padding:12px}} .cards{{display:block}} .card{{margin-bottom:8px}} table{{font-size:11px}} th,td{{padding:6px}}}}
</style>
</head>
<body><div class='wrap'>
<h1>BGL — Fluxo de Caixa VOBI</h1>
<div class='sub'>Atualizado em {generated} · horizonte até {_fmt_date(data['horizon_end'])}</div>
<div class='cards'>
  <div class='card'><small>Saldo atual VOBI</small><strong>{brl(meta['opening_balance'])}</strong></div>
  <div class='card'><small>Menor saldo projetado</small><strong class='{'negative' if meta['minimum_balance'] < 0 else ''}'>{brl(meta['minimum_balance'])}</strong><small>{_fmt_date(meta['minimum_balance_date'])}</small></div>
  <div class='card'><small>Necessidade de caixa</small><strong class='{'negative' if meta['required_cash'] > 0 else ''}'>{brl(meta['required_cash'])}</strong></div>
  <div class='card'><small>A receber · 30 dias</small><strong>{brl(meta['income_30'])}</strong></div>
  <div class='card'><small>A pagar · 30 dias</small><strong>{brl(meta['expense_30'])}</strong></div>
</div>
{warning}
{overdue}

<h2>Semanal · próximas 8 semanas</h2>
<table><thead><tr><th>Período</th><th class='num'>Entradas</th><th class='num'>Saídas</th><th class='num'>Líquido</th><th class='num'>Saldo final</th></tr></thead><tbody>{_table_rows(data['weekly'])}</tbody></table>

<h2>Quinzenal · próximos 6 meses</h2>
<table><thead><tr><th>Período</th><th class='num'>Entradas</th><th class='num'>Saídas</th><th class='num'>Líquido</th><th class='num'>Saldo final</th></tr></thead><tbody>{_table_rows(data['fortnightly'])}</tbody></table>

<h2>Mensal · próximos 12 meses</h2>
<table><thead><tr><th>Período</th><th class='num'>Entradas</th><th class='num'>Saídas</th><th class='num'>Líquido</th><th class='num'>Saldo final</th></tr></thead><tbody>{_table_rows(data['monthly'])}</tbody></table>

<h2>Obras que mais pressionam o caixa · próximos 60 dias</h2>
<table><thead><tr><th>Obra/projeto</th><th>Região</th><th class='num'>Entradas</th><th class='num'>Saídas</th><th class='num'>Líquido</th></tr></thead><tbody>{project_html}</tbody></table>

<h2>Santa Catarina x demais · próximos 60 dias</h2>
<table><thead><tr><th>Grupo</th><th class='num'>Entradas</th><th class='num'>Saídas</th><th class='num'>Líquido</th></tr></thead><tbody>{''.join(region_rows)}</tbody></table>

<h2>Maiores pagamentos · próximos 30 dias</h2>
<table><thead><tr><th>Data</th><th>Obra</th><th>Fornecedor</th><th>Descrição</th><th class='num'>Valor</th></tr></thead><tbody>{expense_html}</tbody></table>

<div class='note'>
<strong>Regras BGL aplicadas:</strong> projeção baseada nas parcelas em aberto do VOBI; parcelas pagas/canceladas não são projetadas; itens de impostos/tributos e “Valmor” são desconsiderados; quando a data de emissão da NF está disponível, pagamentos com prazo inferior a {int(os.getenv('MIN_PAYMENT_LEAD_DAYS', '15'))} dias são deslocados para o prazo mínimo; vencidos em aberto entram na projeção de hoje. Saldo inicial: {html.escape(str(meta['opening_balance_source']))}.<br>
Diagnóstico: {meta['projected_rows']} parcelas projetadas · {meta['ignored_rows']} desconsideradas · {meta['lead_days_adjusted_rows']} ajustadas para prazo mínimo · {meta['unknown_type_rows']} sem classificação receita/despesa.
</div>
</div></body></html>"""


def generate_report():
    data = build_report_data()
    return render_report_html(data), data


def email_configured():
    return all(
        str(os.getenv(name, "")).strip()
        for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "REPORT_RECIPIENT")
    )


def _recipients(value):
    return [item.strip() for item in re.split(r"[,;]", value or "") if item.strip()]


def send_report_email(report_html, data):
    if not email_configured():
        return {"status": "not_configured"}

    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    recipients = _recipients(os.environ["REPORT_RECIPIENT"])
    cc = _recipients(os.getenv("REPORT_CC", ""))
    sender = os.getenv("SMTP_FROM", user)

    if not recipients:
        return {"status": "not_configured"}

    msg = EmailMessage()
    msg["Subject"] = f"BGL | Fluxo de Caixa VOBI | {data['today'].strftime('%d/%m/%Y')}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content("Relatório financeiro BGL gerado automaticamente a partir do VOBI. Abra este e-mail em modo HTML para visualizar o relatório completo.")
    msg.add_alternative(report_html, subtype="html")

    use_ssl = _normalized(os.getenv("SMTP_SSL", "false")) in {"1", "true", "yes", "sim"}
    use_starttls = _normalized(os.getenv("SMTP_STARTTLS", "true")) not in {"0", "false", "no", "nao"}

    if use_ssl:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
    try:
        if not use_ssl and use_starttls:
            smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg, to_addrs=recipients + cc)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    return {"status": "sent", "recipient_count": len(recipients) + len(cc)}


def run_job(send_email=True):
    report_html, data = generate_report()
    email_status = send_report_email(report_html, data) if send_email else {"status": "skipped"}
    return {
        "html": report_html,
        "data": data,
        "email": email_status,
        "meta": _safe_meta(data["meta"]),
    }


def startup_smoke():
    result = {
        "vobi": "not_configured",
        "installments": None,
        "balance_endpoint": None,
        "sample_keys": [],
        "email_configured": email_configured(),
    }
    if not os.getenv("VOBI_UUID") or not os.getenv("VOBI_CLIENT_SECRET"):
        return result

    token = vobi_token()
    result["vobi"] = "authenticated"
    today = datetime.now(TZ).date()
    payload = vobi_get(
        "financial/installments",
        token,
        params={"limit": 1, "offset": 0, "where[initialDate]": (today - timedelta(days=365)).isoformat(), "where[endDate]": _add_months(today, 12).isoformat()},
    )
    result["installments"] = "ok"
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    if rows and isinstance(rows[0], dict):
        result["sample_keys"] = sorted(rows[0].keys())

    try:
        vobi_get("financial/balanceByBankAccount", token)
        result["balance_endpoint"] = "ok"
    except requests.HTTPError as exc:
        result["balance_endpoint"] = f"http_{exc.response.status_code}"
    return result


def safe_log_summary(result):
    return json.dumps(result, ensure_ascii=False, default=str)
