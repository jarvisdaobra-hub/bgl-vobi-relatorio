import json
import os


def _probe():
    if os.getenv("VOBI_PAGINATION_PROBE_ON_START", "0") != "1":
        return
    try:
        from reporting import vobi_get, vobi_token

        token = vobi_token()
        results = []
        for status in range(1, 13):
            payload = vobi_get(
                "financial/installments",
                token,
                params={"limit": 100, "offset": 0, "where[idInstallmentStatus]": status},
            )
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            count = payload.get("count") if isinstance(payload, dict) else None
            returned_statuses = sorted({r.get("idInstallmentStatus") for r in rows})
            tail_rows = None
            if isinstance(count, int) and count >= 9500:
                tail = vobi_get(
                    "financial/installments",
                    token,
                    params={"limit": 500, "offset": 9500, "where[idInstallmentStatus]": status},
                )
                tail_rows = len(tail.get("rows", [])) if isinstance(tail, dict) else None
            results.append({
                "status": status,
                "count": count,
                "sample_rows": len(rows),
                "returned_statuses": returned_statuses,
                "rows_at_9500": tail_rows,
            })
        print("VOBI_STATUS_PARTITION_PROBE " + json.dumps(results, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception as exc:
        print("VOBI_STATUS_PARTITION_PROBE_FAILED " + json.dumps({"type": type(exc).__name__, "message": str(exc)[:200]}, ensure_ascii=False), flush=True)


_probe()
