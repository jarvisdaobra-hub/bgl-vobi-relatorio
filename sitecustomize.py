import json
import os


def _probe():
    if os.getenv("VOBI_PAGINATION_PROBE_ON_START", "0") != "1":
        return
    try:
        from reporting import vobi_get, vobi_token
        token = vobi_token()
        seed = vobi_get(
            "financial/installments",
            token,
            params={"limit": 500, "offset": 0, "where[idInstallmentStatus]": 2},
        )
        rows = seed.get("rows", []) if isinstance(seed, dict) else []
        bank_values = sorted({r.get("idPaymentBankAccount") for r in rows if r.get("idPaymentBankAccount") is not None})
        null_bank_in_sample = sum(1 for r in rows if r.get("idPaymentBankAccount") is None)
        tests = []
        for value in bank_values:
            payload = vobi_get(
                "financial/installments",
                token,
                params={"limit": 100, "offset": 0, "where[idPaymentBankAccount]": value},
            )
            rws = payload.get("rows", []) if isinstance(payload, dict) else []
            returned = sorted({r.get("idPaymentBankAccount") for r in rws if r.get("idPaymentBankAccount") is not None})
            tests.append({
                "count": payload.get("count") if isinstance(payload, dict) else None,
                "sample_rows": len(rws),
                "returned_bank_value_count": len(returned),
                "filter_respected": returned == [value],
                "statuses": sorted({r.get("idInstallmentStatus") for r in rws}),
                "payment_types": sorted({r.get("idPaymentType") for r in rws if r.get("idPaymentType") is not None}),
            })
        print("VOBI_BANK_PARTITION_PROBE " + json.dumps({
            "seed_rows": len(rows),
            "bank_value_count": len(bank_values),
            "null_bank_in_sample": null_bank_in_sample,
            "tests": tests,
        }, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception as exc:
        print("VOBI_BANK_PARTITION_PROBE_FAILED " + json.dumps({"type": type(exc).__name__, "message": str(exc)[:200]}, ensure_ascii=False), flush=True)


_probe()
