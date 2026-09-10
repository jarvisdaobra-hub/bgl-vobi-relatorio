from datetime import datetime

from reporting import TZ, _normalized, vobi_get, vobi_token


def _summarize_payload(payload):
    if isinstance(payload, dict):
        rows = None
        row_key = None
        for key in ("rows", "data", "items", "refurbishes", "projects"):
            if isinstance(payload.get(key), list):
                rows = payload.get(key)
                row_key = key
                break
        if rows is not None:
            sample = []
            for row in rows[:20]:
                if isinstance(row, dict):
                    sample.append({k: row.get(k) for k in row.keys() if str(k).lower() in {"id", "name", "title", "refurbishname", "projectname", "status", "deletedat"}})
                else:
                    sample.append(str(row)[:120])
            return {"kind": "dict_list", "row_key": row_key, "count": payload.get("count"), "rows": len(rows), "sample": sample, "top_keys": sorted(payload.keys())}
        return {"kind": "dict", "top_keys": sorted(payload.keys()), "sample": {k: payload.get(k) for k in list(payload.keys())[:20]}}
    if isinstance(payload, list):
        sample = []
        for row in payload[:20]:
            if isinstance(row, dict):
                sample.append({k: row.get(k) for k in row.keys() if str(k).lower() in {"id", "name", "title", "refurbishname", "projectname", "status", "deletedat"}})
            else:
                sample.append(str(row)[:120])
        return {"kind": "list", "rows": len(payload), "sample": sample}
    return {"kind": type(payload).__name__, "sample": str(payload)[:300]}


def _probe_endpoints(token):
    paths = [
        "refurbishes",
        "refurbish",
        "projects",
        "project",
        "company/refurbishes",
        "companies/refurbishes",
        "refurbishes/360172",
        "projects/360172",
    ]
    out = {}
    for path in paths:
        try:
            out[path] = _summarize_payload(vobi_get(path, token, params={"limit": 500, "offset": 0}))
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            out[path] = {"error": type(exc).__name__, "status": status}
    return out


def _fetch_project_installments(token, project_id):
    rows = []
    limit = 500
    offset = 0
    while True:
        payload = vobi_get("financial/installments", token, params={"limit": limit, "offset": offset, "where[idRefurbish]": project_id})
        batch = payload.get("rows", []) if isinstance(payload, dict) else []
        rows.extend(batch)
        if not batch or len(batch) < limit:
            break
        offset += len(batch)
        if offset >= 10000:
            break
    return {
        "project_id": project_id,
        "reported_count": payload.get("count") if isinstance(payload, dict) else None,
        "fetched_rows": len(rows),
        "project_names": sorted({str(r.get("refurbishName") or "") for r in rows if r.get("refurbishName")}),
    }


def build_sc_audit():
    now = datetime.now(TZ)
    token = vobi_token()
    return {
        "generated_at": now.isoformat(),
        "endpoint_probe": _probe_endpoints(token),
        "blumenau_filter_validation": _fetch_project_installments(token, 360172),
    }
