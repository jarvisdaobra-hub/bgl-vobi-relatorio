import base64
import hmac
import os
from functools import wraps

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


@app.get("/health")
def health():
    return jsonify(
        status="ok",
        service="bgl-vobi-relatorio",
        report_loaded=report_html() is not None,
    )


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
