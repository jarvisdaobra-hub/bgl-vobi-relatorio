import base64
import hmac
import os
from functools import wraps

import requests
from flask import Flask, Response, jsonify, request


app = Flask(__name__)


def authorized(username, password):
    expected_user = os.getenv("REPORT_USER", "bgl")
    expected_password = os.getenv("REPORT_PASSWORD", "")
    return (
        bool(expected_password)
        and hmac.compare_digest(username or "", expected_user)
        and hmac.compare_digest(password or "", expected_password)
    )


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not authorized(auth.username, auth.password):
            return Response(
                "Acesso restrito BGL",
                401,
                {"WWW-Authenticate": 'Basic realm="Relatorio BGL"'},
            )
        return view(*args, **kwargs)

    return wrapped


def report_html():
    encoded = os.getenv("REPORT_HTML_B64", "")
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        app.logger.exception("REPORT_HTML_B64 invalido")
        return None


def configured(*names):
    return any(bool(os.getenv(name, "").strip()) for name in names)


def vobi_base_url():
    return os.getenv("VOBI_API_BASE_URL", "https://api.vobi.com.br/v2").rstrip("/")


def vobi_token():
    uuid = os.environ["VOBI_UUID"]
    secret = os.environ["VOBI_CLIENT_SECRET"]
    response = requests.post(
        f"{vobi_base_url()}/auth/token",
        auth=(uuid, secret),
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("jwt")
    if not token:
        raise RuntimeError("Resposta do Vobi sem JWT")
    return token


def vobi_get(path, token):
    response = requests.get(
        f"{vobi_base_url()}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    return response


def run_vobi_smoke_test():
    result = {
        "auth": "not_configured",
        "financial_totals": None,
        "daily_cash_flow": None,
        "installments": None,
    }

    if not configured("VOBI_UUID") or not configured("VOBI_CLIENT_SECRET"):
        app.logger.warning("VOBI_SMOKE auth=not_configured")
        return result

    try:
        token = vobi_token()
        result["auth"] = "authenticated"
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        result["auth"] = f"authentication_failed:{status}"
        app.logger.warning("VOBI_SMOKE auth=%s", result["auth"])
        return result
    except Exception:
        result["auth"] = "connection_failed"
        app.logger.exception("VOBI_SMOKE auth=connection_failed")
        return result

    checks = {
        "financial_totals": "financial/totals",
        "daily_cash_flow": "financial/dailyCashFlow",
        "installments": "financial/installments",
    }
    for key, path in checks.items():
        try:
            response = vobi_get(path, token)
            result[key] = response.status_code
        except Exception:
            result[key] = "connection_failed"
            app.logger.exception("VOBI_SMOKE endpoint=%s connection_failed", path)

    app.logger.warning(
        "VOBI_SMOKE auth=%s financial_totals=%s daily_cash_flow=%s installments=%s",
        result["auth"],
        result["financial_totals"],
        result["daily_cash_flow"],
        result["installments"],
    )
    return result


VOBI_SMOKE = run_vobi_smoke_test()


@app.get("/health")
def health():
    return jsonify(
        status="ok",
        service="bgl-vobi-relatorio",
        report_loaded=report_html() is not None,
        vobi_uuid_configured=configured("VOBI_UUID"),
        vobi_secret_configured=configured("VOBI_CLIENT_SECRET"),
        vobi_smoke=VOBI_SMOKE,
        email_configured=(
            configured("SMTP_HOST")
            and configured("SMTP_USER")
            and configured("SMTP_PASSWORD")
            and configured("REPORT_RECIPIENT")
        ),
    )


@app.get("/vobi/status")
def vobi_status():
    if not configured("VOBI_UUID") or not configured("VOBI_CLIENT_SECRET"):
        return jsonify(status="not_configured"), 503
    try:
        vobi_token()
        return jsonify(status="authenticated"), 200
    except requests.HTTPError as exc:
        return jsonify(status="authentication_failed", http_status=exc.response.status_code), 502
    except Exception:
        app.logger.exception("Falha ao validar API Vobi")
        return jsonify(status="connection_failed"), 502


@app.get("/")
@require_auth
def index():
    html = report_html()
    if html is None:
        return Response(
            "Relatorio ainda nao carregado.",
            status=503,
            mimetype="text/plain",
        )
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
