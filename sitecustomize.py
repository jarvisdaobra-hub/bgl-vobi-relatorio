import json
import os


def _probe():
    if os.getenv("VOBI_PAGINATION_PROBE_ON_START", "0") != "1":
        return
    try:
        from reporting import vobi_get, vobi_token

        token = vobi_token()
        base_params = {"limit": 500, "offset": 0, "where[idInstallmentStatus]": 2}
        payload = vobi_get("financial/installments", token, params=base_params)
        rows = payload.get("rows", []) if isinstance(payload, dict) else []

        payment_type_values = sorted({r.get("idPaymentType") for r in rows if r.get("idPaymentType") is not None})
        bank_values = sorted({r.get("idPaymentBankAccount") for r in rows if r.get("idPaymentBankAccount") is not None})
        nested_bill_types = sorted({
            (r.get("payment") or {}).get("billType")
            for r in rows
            if isinstance(r.get("payment"), dict) and (r.get("payment") or {}).get("billType") is not None
        })

        tests = []
        for value in payment_type_values:
            p = vobi_get(
                "financial/installments",
                token,
                params={
                    "limit": 100,
                    "offset": 0,
                    "where[idInstallmentStatus]": 2,
                    "where[idPaymentType]": value,
                },
            )
            rws = p.get("rows", []) if isinstance(p, dict) else []
            tests.append({
                "filter": "where[idPaymentType]",
                "value": value,
                "count": p.get("count") if isinstance(p, dict) else None,
                "sample_rows": len(rws),
                "returned_values": sorted({r.get("idPaymentType") for r in rws}),
                "returned_statuses": sorted({r.get("idInstallmentStatus") for r in rws}),
            })

        bill_filter_tests = []
        for key in ("where[billType]", "where[paymentType]", "where[payment.billType]"):
            for value in nested_bill_types:
                p = vobi_get(
                    "financial/installments",
                    token,
                    params={
                        "limit": 100,
                        "offset": 0,
                        "where[idInstallmentStatus]": 2,
                        key: value,
                    },
                )
                rws = p.get("rows", []) if isinstance(p, dict) else []
                returned = sorted({
                    (r.get("payment") or {}).get("billType")
                    for r in rws
                    if isinstance(r.get("payment"), dict) and (r.get("payment") or {}).get("billType") is not None
                })
                bill_filter_tests.append({
                    "filter": key,
                    "value": value,
                    "count": p.get("count") if isinstance(p, dict) else None,
                    "sample_rows": len(rws),
                    "returned_bill_types": returned,
                })

        summary = {
            "status2_count": payload.get("count") if isinstance(payload, dict) else None,
            "sample_rows": len(rows),
            "payment_type_values": payment_type_values,
            "bank_account_value_count": len(bank_values),
            "nested_bill_types": nested_bill_types,
            "payment_type_tests": tests,
            "bill_type_tests": bill_filter_tests,
        }
        print("VOBI_SECOND_PARTITION_PROBE " + json.dumps(summary, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception as exc:
        print("VOBI_SECOND_PARTITION_PROBE_FAILED " + json.dumps({"type": type(exc).__name__, "message": str(exc)[:200]}, ensure_ascii=False), flush=True)


_probe()
