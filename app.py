import hmac
import os
import time
import unicodedata
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request

from reporting import (
    fetch_installments,
    generate_report,
    normalize_installment,
    run_job,
    safe_log_summary,
    startup_smoke,
    vobi_token,
)


app = Flask(__name__)
_CACHE = {"html": None, "data": None, "created": 0.0}
TZ = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/Sao_Paulo"))


def configured(name):
    return bool(str(os.getenv(name, "")).strip())


def authorized(username, password):
    expected_user = os.getenv("REPORT_USER", "bgl")
    expected_password = os.getenv("REPORT_PASSWORD", "")
    return (
        bool(expected_password)
        and hmac.compare_digest(username or "", expected_user)
        and hmac.compare_digest(password or "", expected_password)
    )


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not authorized(auth.username, auth.password):
            return Response(
                "Acesso restrito BGL",
                401,
                {"WWW-Authenticate": 'Basic realm="Relatorio BGL"'},
            )
        return view(*args, **kwargs)

    return wrapped


def internal_authorized():
    expected = os.getenv("JOB_TOKEN", "")
    header = request.headers.get("Authorization", "")
    supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    return bool(expected) and hmac.compare_digest(supplied, expected)


def cached_report(force=False):
    ttl = max(0, int(os.getenv("REPORT_CACHE_SECONDS", "300")))
    now = time.time()
    if not force and _CACHE["html"] is not None and now - _CACHE["created"] <= ttl:
        return _CACHE["html"], _CACHE["data"]

    report_html, data = generate_report()
    _CACHE.update(html=report_html, data=data, created=now)
    return report_html, data


def safe_result(result):
    meta = result.get("meta", {})
    return {
        "status": "ok",
        "email": result.get("email", {}),
        "meta": {
            "generated_at": meta.get("generated_at"),
            "projected_rows": meta.get("projected_rows"),
            "ignored_rows": meta.get("ignored_rows"),
            "lead_days_adjusted_rows": meta.get("lead_days_adjusted_rows"),
            "unknown_type_rows": meta.get("unknown_type_rows"),
            "opening_balance_source": meta.get("opening_balance_source"),
        },
    }


def unassigned_expense_breakdown():
    today = datetime.now(TZ).date()
    end_date = today + timedelta(days=60)
    token = vobi_token()
    rows, _ = fetch_installments(token, today - timedelta(days=365), end_date)
    items = []

    for row in rows:
        item = normalize_installment(row, today)
        if item["paid"] or item["cancelled"] or item["ignored"]:
            continue
        if item["bill_type"] != "expense" or not item["effective_date"]:
            continue
        if item["effective_date"] > end_date:
            continue
        if item["project"] != "Sem obra/projeto":
            continue
        items.append(
            {
                "id": item["id"],
                "date": item["effective_date"].isoformat(),
                "counterparty": item["counterparty"],
                "description": item["description"],
                "amount": item["amount"],
                "overdue": item["overdue"],
            }
        )

    items.sort(key=lambda row: row["amount"], reverse=True)
    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "count": len(items),
        "total": sum(row["amount"] for row in items),
        "items": items,
    }


def _fold(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def receivable_audit_breakdown():
    today = datetime.now(TZ).date()
    token = vobi_token()
    rows, _ = fetch_installments(token, today - timedelta(days=365), today + timedelta(days=120))
    sep_window = []
    sc_candidates = []
    large_candidates = []
    exact_sep7 = []

    for row in rows:
        item = normalize_installment(row, today)
        raw_due = str(row.get("dueDate") or "")[:10]
        raw_payment = row.get("payment") if isinstance(row.get("payment"), dict) else {}
        raw_bill_type = row.get("billType") or raw_payment.get("billType")
        audit = {
            "id": item["id"],
            "raw_due_date": raw_due,
            "effective_date": item["effective_date"].isoformat() if item["effective_date"] else None,
            "status": item["status"],
            "bill_type": item["bill_type"],
            "raw_bill_type": raw_bill_type,
            "project": item["project"],
            "counterparty": item["counterparty"],
            "description": item["description"],
            "amount": item["amount"],
            "paid": item["paid"],
            "cancelled": item["cancelled"],
            "ignored": item["ignored"],
            "overdue": item["overdue"],
        }

        if raw_due == "2026-09-07":
            exact_sep7.append(audit)

        if item["bill_type"] != "income":
            continue

        if "2026-09-01" <= raw_due <= "2026-09-12":
            sep_window.append(audit)

        haystack = _fold(" ".join([item["project"], item["counterparty"], item["description"]]))
        if (
            "2026-08-01" <= raw_due <= "2026-10-31"
            and item["amount"] >= 10000
            and any(term in haystack for term in ["blumenau", "sao jose", "eletrobras", "eletrosul", "axia", "cgtee"])
        ):
            sc_candidates.append(audit)

        if (
            "2026-08-01" <= raw_due <= "2026-10-31"
            and item["amount"] >= 50000
            and not item["cancelled"]
        ):
            large_candidates.append(audit)

    for collection in (sep_window, sc_candidates, large_candidates, exact_sep7):
        collection.sort(key=lambda row: (row["raw_due_date"] or "", -row["amount"]))

    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "sep_window_count": len(sep_window),
        "sep_window": sep_window[:50],
        "exact_sep7_count": len(exact_sep7),
        "exact_sep7": exact_sep7[:20],
        "sc_candidate_count": len(sc_candidates),
        "sc_candidates": sc_candidates[:40],
        "large_candidate_count": len(large_candidates),
        "large_candidates": large_candidates[:40],
    }


try:
    STARTUP_SMOKE = startup_smoke()
    app.logger.warning("STARTUP_SMOKE %s", safe_log_summary(STARTUP_SMOKE))
except Exception as exc:
    STARTUP_SMOKE = {"vobi": "failed", "error_type": type(exc).__name__}
    app.logger.exception("STARTUP_SMOKE failed")

try:
    startup_html, startup_data = generate_report()
    _CACHE.update(html=startup_html, data=startup_data, created=time.time())
    startup_meta = startup_data.get("meta", {})
    top_projects = [
        {
            "name": row.get("name"),
            "income": row.get("income"),
            "expense": row.get("expense"),
            "net": row.get("net"),
            "region": row.get("region"),
        }
        for row in startup_data.get("projects", [])[:5]
    ]
    REPORT_SMOKE = {
        "status": "ok",
        "generated_at": startup_meta.get("generated_at"),
        "projected_rows": startup_meta.get("projected_rows"),
        "ignored_rows": startup_meta.get("ignored_rows"),
        "unknown_type_rows": startup_meta.get("unknown_type_rows"),
        "opening_balance_source": startup_meta.get("opening_balance_source"),
        "opening_balance": startup_meta.get("opening_balance"),
        "minimum_balance": startup_meta.get("minimum_balance"),
        "minimum_balance_date": startup_meta.get("minimum_balance_date"),
        "required_cash": startup_meta.get("required_cash"),
        "final_balance_12m": startup_meta.get("final_balance_12m"),
        "income_30": startup_meta.get("income_30"),
        "expense_30": startup_meta.get("expense_30"),
        "overdue_income": startup_meta.get("overdue_income"),
        "overdue_expense": startup_meta.get("overdue_expense"),
        "overdue_income_count": startup_meta.get("overdue_income_count"),
        "overdue_expense_count": startup_meta.get("overdue_expense_count"),
        "lead_days_adjusted_rows": startup_meta.get("lead_days_adjusted_rows"),
        "regions": startup_data.get("regions", {}),
        "top_projects": top_projects,
    }
    app.logger.warning("REPORT_SMOKE %s", safe_log_summary(REPORT_SMOKE))
except Exception as exc:
    REPORT_SMOKE = {"status": "failed", "error_type": type(exc).__name__}
    app.logger.exception("REPORT_SMOKE failed")

try:
    UNASSIGNED_SMOKE = unassigned_expense_breakdown()
    app.logger.warning("UNASSIGNED_SMOKE %s", safe_log_summary(UNASSIGNED_SMOKE))
except Exception as exc:
    UNASSIGNED_SMOKE = {"status": "failed", "error_type": type(exc).__name__}
    app.logger.exception("UNASSIGNED_SMOKE failed")

try:
    RECEIVABLE_SMOKE = receivable_audit_breakdown()
    app.logger.warning("RECEIVABLE_SMOKE %s", safe_log_summary(RECEIVABLE_SMOKE))
except Exception as exc:
    RECEIVABLE_SMOKE = {"status": "failed", "error_type": type(exc).__name__}
    app.logger.exception("RECEIVABLE_SMOKE failed")


@app.get("/health")
def health():
    return jsonify(
        status="ok",
        service="bgl-vobi-relatorio",
        vobi=STARTUP_SMOKE.get("vobi"),
        installments=STARTUP_SMOKE.get("installments"),
        balance_endpoint=STARTUP_SMOKE.get("balance_endpoint"),
        email_configured=STARTUP_SMOKE.get("email_configured", False),
        job_token_configured=configured("JOB_TOKEN"),
        report_smoke=REPORT_SMOKE,
        sample_key_count=len(STARTUP_SMOKE.get("sample_keys", [])),
    )


@app.get("/")
@require_auth
def index():
    try:
        report_html, _ = cached_report(force=False)
        return Response(report_html, mimetype="text/html")
    except Exception:
        app.logger.exception("Falha ao gerar relatorio Vobi")
        return Response("Falha ao gerar relatorio VOBI.", status=502, mimetype="text/plain")


@app.get("/report/status")
@require_auth
def report_status():
    try:
        _, data = cached_report(force=False)
        meta = data["meta"]
        return jsonify(
            status="ok",
            generated_at=meta.get("generated_at"),
            projected_rows=meta.get("projected_rows"),
            ignored_rows=meta.get("ignored_rows"),
            unknown_type_rows=meta.get("unknown_type_rows"),
            opening_balance_source=meta.get("opening_balance_source"),
        )
    except Exception:
        app.logger.exception("Falha no status do relatorio")
        return jsonify(status="failed"), 502


@app.post("/run")
@require_auth
def manual_run():
    try:
        result = run_job(send_email=True)
        _CACHE.update(html=result["html"], data=result["data"], created=time.time())
        payload = safe_result(result)
        email_status = result.get("email", {}).get("status")
        return jsonify(payload), 200 if email_status in {"sent", "not_configured"} else 503
    except Exception as exc:
        app.logger.exception("Falha na execucao manual do relatorio")
        return jsonify(status="failed", error_type=type(exc).__name__), 502


@app.post("/internal/run")
def internal_run():
    if not internal_authorized():
        return jsonify(status="unauthorized"), 401
    try:
        result = run_job(send_email=True)
        _CACHE.update(html=result["html"], data=result["data"], created=time.time())
        payload = safe_result(result)
        app.logger.warning(
            "REPORT_JOB email=%s projected=%s ignored=%s unknown_type=%s balance_source=%s",
            result.get("email", {}).get("status"),
            result.get("meta", {}).get("projected_rows"),
            result.get("meta", {}).get("ignored_rows"),
            result.get("meta", {}).get("unknown_type_rows"),
            result.get("meta", {}).get("opening_balance_source"),
        )
        email_status = result.get("email", {}).get("status")
        return jsonify(payload), 200 if email_status in {"sent", "not_configured"} else 503
    except Exception as exc:
        app.logger.exception("Falha no job automatico do relatorio")
        return jsonify(status="failed", error_type=type(exc).__name__), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
