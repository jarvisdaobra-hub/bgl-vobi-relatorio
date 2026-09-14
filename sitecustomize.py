import json
import os


def _probe():
    if os.getenv("VOBI_PAGINATION_PROBE_ON_START", "0") != "1":
        return
    try:
        from reporting import vobi_get, vobi_token
        token = vobi_token()

        candidates = [
            {"order": "paidDate DESC"},
            {"order": "paidDate:desc"},
            {"sort": "-paidDate"},
            {"sort": "paidDate", "direction": "desc"},
            {"sortBy": "paidDate", "sortOrder": "desc"},
            {"orderBy": "paidDate", "orderDirection": "desc"},
            {"order[paidDate]": "desc"},
            {"orderBy[paidDate]": "desc"},
        ]

        tests = []
        for extra in candidates:
            params = {"limit": 100, "offset": 0, "where[idInstallmentStatus]": 2}
            params.update(extra)
            payload = vobi_get("financial/installments", token, params=params)
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            paid_dates = [str(r.get("paidDate"))[:10] for r in rows if r.get("paidDate")]
            tests.append({
                "params": extra,
                "count": payload.get("count") if isinstance(payload, dict) else None,
                "sample_rows": len(rows),
                "statuses": sorted({r.get("idInstallmentStatus") for r in rows}),
                "paid_min": min(paid_dates) if paid_dates else None,
                "paid_max": max(paid_dates) if paid_dates else None,
                "first_paid_dates": paid_dates[:5],
            })

        print("VOBI_SORT_PROBE " + json.dumps(tests, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception as exc:
        print("VOBI_SORT_PROBE_FAILED " + json.dumps({"type": type(exc).__name__, "message": str(exc)[:200]}, ensure_ascii=False), flush=True)


_probe()
