"""Autorização OAuth do Mercado Livre — roda uma vez.

O ML usa authorization_code: é preciso abrir uma página, aprovar o app com a sua
conta e devolver o `code` para a aplicação. Este script sobe um servidor local só
para capturar essa volta, troca o code por tokens e grava o ML_REFRESH_TOKEN no
.env. Depois disso o provider renova sozinho.

O ML **exige HTTPS** na URI de redirect — `http://localhost` é recusado na
validação do formulário do DevCenter. Por isso este script sobe um servidor
HTTPS local com certificado autoassinado, gerado na hora em `.certs/`.

O navegador vai avisar que o certificado não é confiável. É esperado: o
certificado é seu, gerado agora, para uma conexão que não sai da sua máquina.
Clique em "Avançado" → "Prosseguir para localhost".

Antes de rodar, no DevCenter (developers.mercadolivre.com.br/devcenter):
  1. Criar aplicação
  2. Na seção "Autenticação e segurança", campo "Redirect URI", cadastrar:
         https://localhost:8788/callback
  3. Copiar Client ID e Client Secret para o .env:
         ML_CLIENT_ID=...
         ML_CLIENT_SECRET=...

    uv run python scripts/ml_auth.py
"""
from __future__ import annotations

import http.server
import os
import pathlib
import socketserver
import ssl
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402

from game_deals import config                                  # noqa: E402
from game_deals.providers.mercadolivre import salva_refresh_token  # noqa: E402

AUTORIZA = "https://auth.mercadolivre.com.br/authorization"
TOKEN = "https://api.mercadolibre.com/oauth/token"
PORTA = 8788

_codigo: dict[str, str] = {}

RAIZ = pathlib.Path(__file__).resolve().parents[1]
CERTS = RAIZ / ".certs"


def certificado() -> tuple[str, str]:
    """Certificado autoassinado para localhost, gerado uma vez e reaproveitado."""
    CERTS.mkdir(exist_ok=True)
    cert, chave = CERTS / "localhost.crt", CERTS / "localhost.key"
    if cert.exists() and chave.exists():
        return str(cert), str(chave)

    print("Gerando certificado autoassinado para localhost...")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(chave), "-out", str(cert), "-days", "825",
        "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, capture_output=True)
    chave.chmod(0o600)
    return str(cert), str(chave)


class Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _codigo["code"] = (q.get("code") or [""])[0]
        _codigo["error"] = (q.get("error_description") or q.get("error") or [""])[0]
        ok = bool(_codigo["code"])
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("<h2>Pronto — pode fechar esta aba.</h2>" if ok else
               f"<h2>Falhou</h2><p>{_codigo['error']}</p>")
        self.wfile.write(f"<html><body style='font-family:system-ui;padding:40px'>"
                         f"{msg}</body></html>".encode())
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *a):
        pass


def main() -> None:
    if not (config.ML_CLIENT_ID and config.ML_CLIENT_SECRET):
        sys.exit("Falta ML_CLIENT_ID / ML_CLIENT_SECRET no .env. "
                 "Veja o cabeçalho deste arquivo.")

    redirect = config.ML_REDIRECT_URI
    url = (f"{AUTORIZA}?response_type=code&client_id={config.ML_CLIENT_ID}"
           f"&redirect_uri={urllib.parse.quote(redirect, safe='')}")

    print("Abrindo o navegador para você aprovar o app...\n")
    print(f"  Se não abrir sozinho, cole no navegador:\n  {url}\n")
    print(f"  A URI de redirect precisa estar cadastrada no DevCenter como:")
    print(f"      {redirect}\n")

    cert, chave = certificado()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, chave)

    socketserver.TCPServer.allow_reuse_address = True
    servidor = socketserver.TCPServer(("127.0.0.1", PORTA), Callback)
    servidor.socket = ctx.wrap_socket(servidor.socket, server_side=True)

    print("  O navegador vai avisar que o certificado nao e confiavel — é")
    print("  esperado: ele é seu, gerado agora, e a conexão não sai da máquina.")
    print("  Clique em 'Avançado' → 'Prosseguir para localhost'.\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Aguardando a volta em {redirect} ...")
    servidor.serve_forever()
    servidor.server_close()

    if not _codigo.get("code"):
        sys.exit(f"Não veio código. {_codigo.get('error', '')}")

    r = httpx.post(TOKEN, timeout=30, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"}, data={
        "grant_type": "authorization_code",
        "client_id": config.ML_CLIENT_ID,
        "client_secret": config.ML_CLIENT_SECRET,
        "code": _codigo["code"],
        "redirect_uri": redirect})

    if r.status_code >= 400:
        sys.exit(f"Troca do código falhou: {r.status_code} {r.text[:300]}")

    j = r.json()
    refresh = j.get("refresh_token")
    if not refresh:
        sys.exit(f"Resposta sem refresh_token: {str(j)[:200]}")

    salva_refresh_token(refresh)
    print("\n✓ ML_REFRESH_TOKEN gravado no .env (permissão 600).")
    print(f"  access_token válido por {j.get('expires_in')}s; "
          f"o provider renova sozinho a partir de agora.")
    print("\nTeste:  uv run python -c \"from game_deals import providers; "
          "print(providers.get('mercadolivre').search('elden ring ps5', 3))\"")


if __name__ == "__main__":
    main()
