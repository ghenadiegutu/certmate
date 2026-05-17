#!/usr/bin/env python3
"""
Salt Webhook Relay Server
=========================
Gira sull'host, riceve le notifiche di CertMate (deploy hook)
e triggera il deploy Salt.

Avvio:
    CERTMATE_URL=http://localhost:8000 \
    CERTMATE_TOKEN=<token> \
    SALT_API_PASSWORD=SaltApi2026! \
    python3 scripts/salt_webhook_server.py

Ascolta su 0.0.0.0:5001 — raggiungibile da Docker via 172.17.0.1:5001
"""

import os
import sys
import json
import logging
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger('salt_webhook_server')

PORT = int(os.environ.get('WEBHOOK_PORT', 5001))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def run_deploy(domain: str):
    """Esegue salt_deploy.py in background."""
    env = os.environ.copy()
    log.info(f"Triggering deploy per: {domain}")
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, 'salt_deploy.py'), domain],
            env=env,
            capture_output=True,
            text=True,
            timeout=180
        )
        if result.returncode == 0:
            log.info(f"Deploy OK per {domain}")
        else:
            log.error(f"Deploy FALLITO per {domain}:\n{result.stderr}")
    except subprocess.TimeoutExpired:
        log.error(f"Deploy TIMEOUT per {domain}")
    except Exception as e:
        log.error(f"Errore deploy {domain}: {e}")


class WebhookHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.info(f"{self.client_address[0]} {format % args}")

    def do_POST(self):
        if self.path != '/deploy':
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"Not found"}')
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8', errors='replace')

        # Accetta JSON o form-encoded
        domain = ''
        try:
            data = json.loads(body)
            domain = data.get('domain', '').strip()
        except (json.JSONDecodeError, ValueError):
            # form-encoded: domain=xxx
            for part in body.split('&'):
                k, _, v = part.partition('=')
                if k.strip() == 'domain':
                    domain = v.strip()
                    break

        if not domain:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"domain required"}')
            return

        # Avvia deploy in thread separato (non blocca la risposta)
        t = threading.Thread(target=run_deploy, args=(domain,), daemon=True)
        t.start()

        self.send_response(202)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        resp = json.dumps({'status': 'accepted', 'domain': domain})
        self.wfile.write(resp.encode())

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    log.info(f"Salt webhook server avviato su 0.0.0.0:{PORT}")
    log.info(f"CertMate deploy hook: curl -X POST http://172.17.0.1:{PORT}/deploy -d domain=$CERTMATE_DOMAIN")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server fermato")
