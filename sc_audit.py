from datetime import datetime

from reporting import TZ, _normalized, vobi_get, vobi_token


def _id_fields(obj):
    if not isinstance(obj, dict):
        return {}
    out = {}
    for key, value in obj.items():
        k = str(key).lower()
        if "id" in k or "refurb" in k or "project" in k or key == "_meta":
            if isinstance(value, dict):
                out[key] = _id_fields(value)
            elif isinstance(value, list):
                out[key] = value[:3]
            else:
                out[key] = value
    return out


def _scan_matches(token):
    matches = {"blumenau": [], "sao_jose": [], "valmor": []}
    limit = 500
    for offset in range(0, 10000, limit):
        payload = vobi_get("financial/installments", token, params={"limit": limit, "offset": offset})
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        for row in rows:
            text = _normalized(" ".join(str(row.get(k) or "") for k in ("refurbishName", "description", "supplierName", "name")))
            bucket = None
            if "blumenau" in text:
                bucket = "blumenau"
            elif "sao jose" in text:
                bucket = "sao_jose"
            elif "valmor" in text:
                bucket = "valmor"
            if bucket and len(matches[bucket]) < 5:
                matches[bucket].append({
                    "offset_page": offset,
                    "refurbishName": row.get("refurbishName"),
                    "description": row.get("description"),
                    "supplierName": row.get("supplierName"),
                    "dueDate": row.get("dueDate"),
                    "status": row.get("idInstallmentStatus"),
                    "id_fields": _id_fields(row),
                    "top_keys": sorted(row.keys()),
                })
        if all(matches[k] for k in ("blumenau", "sao_jose")):
            # Continue a little to pick up Valmor if possible, but no need to scan all pages once IDs are found.
            if matches["valmor"] or offset >= 5000:
                break
        if len(rows) < limit:
            break
    return matches


def build_sc_audit():
    now = datetime.now(TZ)
    token = vobi_token()
    return {
        "generated_at": now.isoformat(),
        "internal_id_probe": _scan_matches(token),
    }
