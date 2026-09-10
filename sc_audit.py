from datetime import datetime

from reporting import TZ, vobi_get, vobi_token


def _probe_status(token, params):
    payload = vobi_get("financial/installments", token, params={"limit": 500, "offset": 0, **params})
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    return {
        "count": payload.get("count") if isinstance(payload, dict) else None,
        "rows": len(rows),
        "statuses": sorted({r.get("idInstallmentStatus") for r in rows}),
        "due_min": min((str(r.get("dueDate"))[:10] for r in rows if r.get("dueDate")), default=None),
        "due_max": max((str(r.get("dueDate"))[:10] for r in rows if r.get("dueDate")), default=None),
        "projects": sorted({str(r.get("refurbishName") or "") for r in rows if r.get("refurbishName")})[:10],
    }


def build_sc_audit():
    now = datetime.now(TZ)
    token = vobi_token()
    tests = {}
    variants = {
        "where_status_1": {"where[idInstallmentStatus]": 1},
        "where_status_str_1": {"where[idInstallmentStatus]": "1"},
        "where_status_5": {"where[idInstallmentStatus]": 5},
        "where_status_1_blumenau": {"where[idInstallmentStatus]": 1, "where[idRefurbish]": 360172},
        "where_status_1_sao_jose": {"where[idInstallmentStatus]": 1, "where[idRefurbish]": 320846},
    }
    for name, params in variants.items():
        try:
            tests[name] = _probe_status(token, params)
        except Exception as exc:
            tests[name] = {"error": type(exc).__name__, "status": getattr(getattr(exc, "response", None), "status_code", None)}
    return {"generated_at": now.isoformat(), "status_filter_probe": tests}
