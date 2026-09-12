import hmac
import os
import threading
import time
from functools import wraps

from flask import Flask, Response, jsonify, request

from controller import build_controller_snapshot
from reporting import generate_report, run_job, safe_log_summary, startup_smoke

app = Flask(__name__)
_CACHE = {"html": None, "data": None, "created": 0.0}
_CONTROLLER_REFRESH = {"created": 0.0, "generated_at": None, "status": "never"}
_CONTROLLER_REFRESH_LOCK = threading.Lock()


def configured(name):
    return bool(str(os.getenv(name, "")).strip())


def authorized(username, password):
    expected_user = os.getenv("REPORT_USER", "bgl")
    expected_password = os.getenv("REPORT_PASSWORD", "")
    return bool(expected_password) and hmac.compare_digest(username or "", expected_user) and hmac.compare_digest(password or "", expected_password)


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not authorized(auth.username, auth.password):
            return Response("Acesso restrito BGL", 401, {"WWW-Authenticate": 'Basic realm="Relatorio BGL"'})
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


def personnel_advances_to_sep30(snapshot):
    if not isinstance(snapshot, dict):
        return {"total": 0.0, "by_project": {}, "items": []}

    sources = [
        (snapshot.get("blumenau_items_45d", []), False),
        (snapshot.get("sao_jose_items_45d", []), False),
        (snapshot.get("excluded_items_45d", []), True),
    ]
    terms = ("vale", "adiant", "ajuda de custo", "adiantamento salarial")
    matches = []
    seen = set()

    for items, excluded in sources:
        for item in items or []:
            if item.get("bill_type") != "expense":
                continue
            due = str(item.get("due_date") or "")
            if not due or due > "2026-09-30":
                continue
            project = str(item.get("project") or "")
            project_norm = project.casefold()
            if "blumenau" not in project_norm and "são josé" not in project_norm and "sao jose" not in project_norm:
                continue
            text = f"{item.get('description') or ''} {item.get('counterparty') or ''}".casefold()
            if not any(term in text for term in terms):
                continue
            key = (due, project, item.get("counterparty"), item.get("description"), round(float(item.get("amount") or 0), 2))
            if key in seen:
                continue
            seen.add(key)
            matches.append({
                "due_date": due,
                "project": project,
                "counterparty": item.get("counterparty"),
                "description": item.get("description"),
                "amount": float(item.get("amount") or 0),
                "excluded_by_legacy_rule": excluded,
            })

    by_project = {}
    for item in matches:
        name = item["project"]
        by_project[name] = by_project.get(name, 0.0) + item["amount"]

    return {
        "total": sum(item["amount"] for item in matches),
        "by_project": by_project,
        "items": sorted(matches, key=lambda x: (x["due_date"], x["project"], -x["amount"])),
    }


def controller_log_summary(snapshot):
    if not isinstance(snapshot, dict):
        return {"status": "failed"}

    overall = snapshot.get("overall_45d", {})
    windows = snapshot.get("windows", {})
    excluded = sorted(
        snapshot.get("excluded_items_45d", []) or [],
        key=lambda x: float(x.get("amount") or 0),
        reverse=True,
    )[:15]

    return {
        "status": snapshot.get("status"),
        "generated_at": snapshot.get("generated_at"),
        "source": snapshot.get("source"),
        "opening_balance_source": snapshot.get("opening_balance_source"),
        "opening_balance": snapshot.get("opening_balance"),
        "api_count": snapshot.get("api_count"),
        "fetched_open_rows": snapshot.get("fetched_open_rows"),
        "projected_rows": snapshot.get("projected_rows"),
        "ignored_rows": snapshot.get("ignored_rows"),
        "ignored_value_45d": snapshot.get("ignored_value_45d"),
        "unknown_type_rows": snapshot.get("unknown_type_rows"),
        "missing_date_rows": snapshot.get("missing_date_rows"),
        "paid_rows": snapshot.get("paid_rows"),
        "cancelled_rows": snapshot.get("cancelled_rows"),
        "personnel_advances_to_sep30": personnel_advances_to_sep30(snapshot),
        "windows": {
            key: {
                "days": value.get("days"),
                "end": value.get("end"),
                "income": value.get("income"),
                "expense": value.get("expense"),
                "net": value.get("net"),
                "closing_balance": value.get("closing_balance"),
                "minimum_balance": value.get("minimum_balance"),
                "minimum_balance_date": value.get("minimum_balance_date"),
                "required_cash": value.get("required_cash"),
            }
            for key, value in windows.items()
        },
        "overall_45d": {
            "minimum_balance": overall.get("minimum_balance"),
            "minimum_balance_date": overall.get("minimum_balance_date"),
            "required_cash": overall.get("required_cash"),
            "final_balance_45d": overall.get("final_balance_45d"),
        },
        "flow_45d": snapshot.get("flow_45d"),
        "overdue": snapshot.get("overdue"),
        "payment_rule": snapshot.get("payment_rule"),
        "projects_45d": snapshot.get("projects_45d"),
        "possible_duplicates": snapshot.get("possible_duplicates"),
        "top_expenses_30d": snapshot.get("top_expenses_30d"),
        "financial_items_45d": (snapshot.get("financial_items_45d") or [])[:20],
        "top_excluded_items_45d": excluded,
        "not_available_from_current_vobi_snapshot": snapshot.get("not_available_from_current_vobi_snapshot"),
    }


def refresh_controller_snapshot():
    snapshot = build_controller_snapshot()
    summary = controller_log_summary(snapshot)
    app.logger.warning("CONTROLLER_SNAPSHOT %s", safe_log_summary(summary))
    _CONTROLLER_REFRESH.update(
        created=time.time(),
        generated_at=snapshot.get("generated_at"),
        status=snapshot.get("status", "unknown"),
    )
    return snapshot


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
    REPORT_SMOKE = {
        "status": "ok",
        "generated_at": startup_meta.get("generated_at"),
        "projected_rows": startup_meta.get("projected_rows"),
        "ignored_rows": startup_meta.get("ignored_rows"),
        "unknown_type_rows": startup_meta.get("unknown_type_rows"),
        "opening_balance_source": startup_meta.get("opening_balance_source"),
    }
    app.logger.warning("REPORT_SMOKE %s", safe_log_summary(REPORT_SMOKE))
except Exception as exc:
    REPORT_SMOKE = {"status": "failed", "error_type": type(exc).__name__}
    app.logger.exception("REPORT_SMOKE failed")

try:
    CONTROLLER_SNAPSHOT = refresh_controller_snapshot()
except Exception as exc:
    CONTROLLER_SNAPSHOT = {"status": "failed", "error_type": type(exc).__name__}
    app.logger.exception("CONTROLLER_SNAPSHOT failed")


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
        report_status=REPORT_SMOKE.get("status"),
        report_generated_at=REPORT_SMOKE.get("generated_at"),
        controller_status=CONTROLLER_SNAPSHOT.get("status"),
        controller_generated_at=CONTROLLER_SNAPSHOT.get("generated_at"),
        controller_source=CONTROLLER_SNAPSHOT.get("source"),
        controller_open_rows=CONTROLLER_SNAPSHOT.get("fetched_open_rows"),
        sample_key_count=len(STARTUP_SMOKE.get("sample_keys", [])),
    )


@app.get("/controller/refresh")
def controller_refresh_public():
    min_seconds = max(60, int(os.getenv("CONTROLLER_REFRESH_MIN_SECONDS", "300")))
    age = time.time() - float(_CONTROLLER_REFRESH.get("created") or 0.0)
    if _CONTROLLER_REFRESH.get("generated_at") and age < min_seconds:
        return jsonify(
            status=_CONTROLLER_REFRESH.get("status", "ok"),
            refreshed=False,
            generated_at=_CONTROLLER_REFRESH.get("generated_at"),
        )

    if not _CONTROLLER_REFRESH_LOCK.acquire(blocking=False):
        return jsonify(status="busy", refreshed=False), 202

    try:
        snapshot = refresh_controller_snapshot()
        return jsonify(
            status=snapshot.get("status", "ok"),
            refreshed=True,
            generated_at=snapshot.get("generated_at"),
        )
    except Exception as exc:
        app.logger.exception("CONTROLLER_REFRESH failed")
        return jsonify(status="failed", error_type=type(exc).__name__), 502
    finally:
        _CONTROLLER_REFRESH_LOCK.release()


@app.get("/controller/status")
@require_auth
def controller_status():
    try:
        snapshot = build_controller_snapshot()
        return jsonify(controller_log_summary(snapshot))
    except Exception:
        app.logger.exception("Falha no status do controller")
        return jsonify(status="failed"), 502


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
        return jsonify(status="ok", generated_at=meta.get("generated_at"), projected_rows=meta.get("projected_rows"), ignored_rows=meta.get("ignored_rows"), unknown_type_rows=meta.get("unknown_type_rows"), opening_balance_source=meta.get("opening_balance_source"))
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
        app.logger.warning("REPORT_JOB %s", safe_log_summary(payload))
        email_status = result.get("email", {}).get("status")
        return jsonify(payload), 200 if email_status in {"sent", "not_configured"} else 503
    except Exception as exc:
        app.logger.exception("Falha no job automatico do relatorio")
        return jsonify(status="failed", error_type=type(exc).__name__), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
