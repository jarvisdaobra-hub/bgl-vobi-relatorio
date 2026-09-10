from datetime import datetime

from reporting import TZ, _normalized, vobi_get, vobi_token


def _project_id(row):
    meta = row.get("_meta") if isinstance(row, dict) else None
    refurbishes = meta.get("refurbishes") if isinstance(meta, dict) else None
    ids = refurbishes.get("id") if isinstance(refurbishes, dict) else None
    if isinstance(ids, list) and ids:
        return ids[0]
    return None


def _scan_project_names(token):
    found = []
    seen = set()
    limit = 500
    for offset in range(0, 10000, limit):
        payload = vobi_get("financial/installments", token, params={"limit": limit, "offset": offset})
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        for row in rows:
            project = str(row.get("refurbishName") or "").strip()
            pnorm = _normalized(project)
            if any(term in pnorm for term in ("blumenau", "sao jose", "eletrobras", "eletrobras")):
                key = (project, _project_id(row))
                if key not in seen:
                    seen.add(key)
                    found.append({"project": project, "project_id": _project_id(row), "offset_page": offset})
        if len(rows) < limit:
            break
    return found


def _probe_id_filters(token, project_id):
    variants = {
        "idRefurbish": {"idRefurbish": project_id},
        "refurbishId": {"refurbishId": project_id},
        "projectId": {"projectId": project_id},
        "idProject": {"idProject": project_id},
        "where_idRefurbish": {"where[idRefurbish]": project_id},
        "where_refurbishId": {"where[refurbishId]": project_id},
        "where_projectId": {"where[projectId]": project_id},
        "where_refurbishes_id": {"where[refurbishes.id]": project_id},
        "refurbishes_id": {"refurbishes[id]": project_id},
        "idRefurbishes": {"idRefurbishes": project_id},
        "where_idRefurbishes": {"where[idRefurbishes]": project_id},
    }
    results = {}
    for name, extra in variants.items():
        try:
            payload = vobi_get("financial/installments", token, params={"limit": 100, "offset": 0, **extra})
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            projects = sorted({str(r.get("refurbishName") or "") for r in rows if r.get("refurbishName")})
            ids = sorted({x for x in (_project_id(r) for r in rows) if x is not None})
            results[name] = {
                "count": payload.get("count") if isinstance(payload, dict) else None,
                "rows": len(rows),
                "projects": projects[:10],
                "project_ids": ids[:10],
                "matching_rows": sum(1 for r in rows if _project_id(r) == project_id),
            }
        except Exception as exc:
            results[name] = {"error": type(exc).__name__}
    return results


def build_sc_audit():
    now = datetime.now(TZ)
    token = vobi_token()
    blumenau_id = 360172
    return {
        "generated_at": now.isoformat(),
        "project_names": _scan_project_names(token),
        "blumenau_id_filter_probe": _probe_id_filters(token, blumenau_id),
    }
