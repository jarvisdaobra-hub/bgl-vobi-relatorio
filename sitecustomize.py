import json
import os


def _probe():
    if os.getenv("VOBI_PAGINATION_PROBE_ON_START", "0") != "1":
        return
    try:
        from reporting import vobi_get, vobi_token

        token = vobi_token()
        results = []
        for offset in (0, 9500, 10000, 10500, 15000, 20000):
            payload = vobi_get(
                "financial/installments",
                token,
                params={"limit": 500, "offset": offset},
            )
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            paid_dates = sorted(str(r.get("paidDate"))[:10] for r in rows if r.get("paidDate"))
            due_dates = sorted(str(r.get("dueDate"))[:10] for r in rows if r.get("dueDate"))
            results.append({
                "offset": offset,
                "count": payload.get("count") if isinstance(payload, dict) else None,
                "rows": len(rows),
                "paid_min": paid_dates[0] if paid_dates else None,
                "paid_max": paid_dates[-1] if paid_dates else None,
                "due_min": due_dates[0] if due_dates else None,
                "due_max": due_dates[-1] if due_dates else None,
            })
        print("VOBI_PAGINATION_PROBE " + json.dumps(results, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception as exc:
        print("VOBI_PAGINATION_PROBE_FAILED " + json.dumps({"type": type(exc).__name__, "message": str(exc)[:200]}, ensure_ascii=False), flush=True)


_probe()
