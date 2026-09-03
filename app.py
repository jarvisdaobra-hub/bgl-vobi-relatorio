import hmac
import os
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

def authorized(username, password):
    expected_user = os.getenv("REPORT_USER", "bgl")
    expected_password = os.getenv("REPORT_PASSWORD", "")
    return bool(expected_password) and hmac.compare_digest(username or "", expected_user) and hmac.compare_digest(password or "", expected_password)

def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not authorized(auth.username, auth.password):
            return Response("Acesso restrito BGL", 401, {"WWW-Authenticate": 'Basic realm="Relatorio BGL"'})
        return view(*args, **kwargs)
    return wrapped

@app.get("/health")
def health():
    return jsonify(status="ok", service="bgl-vobi-relatorio")

@app.get("/")
@require_auth
def index():
    generated_at = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    return Response(f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Relatório Financeiro Vobi - BGL</title><style>body{{font-family:Arial,sans-serif;margin:0;background:#f4f6f8;color:#1f2937}}header{{background:#13293d;color:white;padding:28px}}main{{max-width:960px;margin:28px auto;padding:0 18px}}.card{{background:white;border-radius:12px;padding:24px;box-shadow:0 2px 12px #0001}}.ok{{color:#0b7a45;font-weight:700}}small{{color:#64748b}}</style></head><body><header><h1>Relatório Financeiro Vobi — BGL</h1></header><main><section class="card"><p class="ok">✓ Serviço online e protegido</p><p>A estrutura está pronta para a coleta automática dos dados do Vobi.</p><small>Verificação: {{generated_at}}</small></section></main></body></html>""", mimetype="text/html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
