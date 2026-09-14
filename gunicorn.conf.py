import os
import threading
import time


def _run_mcmv_audit():
    time.sleep(8)
    try:
        from mcmv_history import previous_month_mcmv_cash
        previous_month_mcmv_cash()
    except Exception as exc:
        print(f"MCMV_PREVIOUS_MONTH_CASH_FAILED {type(exc).__name__}: {exc}", flush=True)


def _run_admin_audit():
    time.sleep(8)
    try:
        from admin_history import previous_month_admin_cash
        previous_month_admin_cash()
    except Exception as exc:
        print(f"ADMIN_PREVIOUS_MONTH_CASH_FAILED {type(exc).__name__}: {exc}", flush=True)


def when_ready(server):
    if str(os.getenv("MCMV_AUDIT_ON_START", "")).strip() == "1":
        thread = threading.Thread(target=_run_mcmv_audit, name="mcmv-audit", daemon=True)
        thread.start()
    if str(os.getenv("ADMIN_AUDIT_ON_START", "")).strip() == "1":
        thread = threading.Thread(target=_run_admin_audit, name="admin-audit", daemon=True)
        thread.start()
