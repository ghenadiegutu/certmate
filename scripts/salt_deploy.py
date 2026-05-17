#!/usr/bin/env python3
"""
Salt Deploy Script per CertMate
================================
Viene chiamato dal deploy hook di CertMate dopo il rinnovo di un certificato.

Utilizzo:
    python3 salt_deploy.py <domain>

Esempio:
    python3 salt_deploy.py demo.certmate.local

Variabili d'ambiente richieste:
    CERTMATE_URL        URL di CertMate (es. http://localhost:8000)
    CERTMATE_TOKEN      API token di CertMate
    SALT_API_PASSWORD   Password utente salt-api

Variabili opzionali:
    SALT_API_USER       Utente salt-api (default: saltapi)
    SALT_API_EAUTH      Metodo auth (default: pam)
"""

import os
import sys
import json
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger('salt_deploy')

# ── Configurazione ──────────────────────────────────────────────────────────
CERTMATE_URL   = os.environ.get('CERTMATE_URL', 'http://localhost:8000')
CERTMATE_TOKEN = os.environ.get('CERTMATE_TOKEN', '')
SALT_API_USER  = os.environ.get('SALT_API_USER', 'saltapi')
SALT_API_PASS  = os.environ.get('SALT_API_PASSWORD', 'SaltApi2026!')
SALT_API_EAUTH = os.environ.get('SALT_API_EAUTH', 'pam')


def get_salt_metadata(domain: str) -> dict:
    """Legge i metadata Salt dal webhook di CertMate."""
    url = f"{CERTMATE_URL}/api/webhook/cert-renewed"
    resp = requests.post(
        url,
        json={'cert_name': domain},
        headers={'Authorization': f'Bearer {CERTMATE_TOKEN}'},
        timeout=10
    )
    if resp.status_code == 404:
        raise ValueError(f"Nessun metadata Salt per '{domain}'. Configura la sezione Salt in CertMate.")
    resp.raise_for_status()
    return resp.json()


def salt_api_login(api_url: str) -> str:
    """Login a salt-api, restituisce il token."""
    resp = requests.post(
        f"{api_url}/login",
        json={'username': SALT_API_USER, 'password': SALT_API_PASS, 'eauth': SALT_API_EAUTH},
        headers={'Content-Type': 'application/json'},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    token = data['return'][0]['token']
    log.info(f"Login salt-api OK (token: {token[:12]}...)")
    return token


def salt_run_state(api_url: str, token: str, minions: list, domain: str, service: str) -> dict:
    """Esegue state.apply sui minion tramite salt-api."""
    resp = requests.post(
        api_url,
        json=[{
            'client': 'local',
            'tgt': ','.join(minions),
            'tgt_type': 'list',
            'fun': 'state.apply',
            'arg': ['certmate.deploy_cert'],
            'kwarg': {
                'pillar': {
                    'certmate_domain': domain,
                    'certmate_url': CERTMATE_URL,
                    'certmate_token': CERTMATE_TOKEN,
                    'service_restart': service,
                }
            }
        }],
        headers={
            'X-Auth-Token': token,
            'Content-Type': 'application/json'
        },
        timeout=120
    )
    resp.raise_for_status()
    return resp.json()


def deploy(domain: str):
    log.info(f"=== Deploy Salt per: {domain} ===")

    # 1. Leggi metadata
    log.info("Lettura metadata da CertMate...")
    meta = get_salt_metadata(domain)
    log.info(f"  Masters  : {meta['salt_masters']}")
    log.info(f"  Minions  : {meta['minions']}")
    log.info(f"  Ambiente : {meta['environment']}")
    log.info(f"  Servizio : {meta['service_restart']}")

    if not meta.get('deploy_enabled', True):
        log.warning("Deploy disabilitato nei metadata. Uscita.")
        return

    if not meta.get('minions'):
        log.warning("Nessun minion configurato. Uscita.")
        return

    # 2. Per ogni Salt Master configurato
    errors = []
    for master_id in meta['salt_masters']:
        # Ricava URL del master dai metadata (o usa l'id direttamente come hostname)
        # In produzione leggi da config/salt_masters.json
        salt_api_url = _resolve_master_url(master_id)
        log.info(f"Connessione a Salt Master: {master_id} ({salt_api_url})")

        try:
            token = salt_api_login(salt_api_url)
            result = salt_run_state(
                salt_api_url, token,
                meta['minions'], domain,
                meta.get('service_restart', 'nginx')
            )
            _log_state_result(result, meta['minions'])
        except Exception as e:
            log.error(f"Errore su {master_id}: {e}")
            errors.append(f"{master_id}: {e}")

    if errors:
        log.error(f"Completato con errori: {errors}")
        sys.exit(1)
    else:
        log.info(f"=== Deploy completato per {domain} ===")


def _resolve_master_url(master_id: str) -> str:
    """Risolve il master_id in URL API leggendo config/salt_masters.json.
    Fallback: usa master_id come hostname con porta 8080.
    """
    config_paths = [
        os.path.join(os.path.dirname(__file__), '..', 'config', 'salt_masters.json'),
        '/app/config/salt_masters.json',
    ]
    for path in config_paths:
        try:
            with open(path) as f:
                cfg = json.load(f)
            for m in cfg.get('masters', []):
                if m['id'] == master_id:
                    host = m.get('host', master_id)
                    port = m.get('port', 8080)
                    return f"http://{host}:{port}"
        except (FileNotFoundError, KeyError):
            continue
    # fallback: usa master_id come hostname
    return f"http://{master_id}:8080"


def _log_state_result(result: dict, minions: list):
    """Logga il risultato dello state.apply."""
    returns = result.get('return', [{}])[0]
    for minion in minions:
        minion_result = returns.get(minion, {})
        if isinstance(minion_result, dict):
            failed = [k for k, v in minion_result.items()
                      if isinstance(v, dict) and not v.get('result', True)]
            if failed:
                log.error(f"  {minion}: {len(failed)} state(s) falliti: {failed}")
            else:
                changed = sum(1 for v in minion_result.values()
                              if isinstance(v, dict) and v.get('changes'))
                log.info(f"  {minion}: OK ({changed} cambiamenti)")
        else:
            log.warning(f"  {minion}: risposta inattesa: {minion_result}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Uso: {sys.argv[0]} <domain>")
        sys.exit(1)
    deploy(sys.argv[1])
