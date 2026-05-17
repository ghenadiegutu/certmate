"""
Salt Stack integration for CertMate.
Handles salt-api authentication and certificate deployment.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_TOKEN_TTL = 3600  # Re-login after 1 hour


class SaltManager:
    """Manages Salt Stack deployments triggered by certificate events."""

    def __init__(self, settings_manager, cert_dir: Path):
        self.settings_manager = settings_manager
        self.cert_dir = Path(cert_dir)
        # token cache: {master_id: {"token": str, "ts": float}}
        self._tokens: dict = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Config                                                              #
    # ------------------------------------------------------------------ #

    def get_config(self) -> dict:
        settings = self.settings_manager.load_settings()
        return settings.get('salt', {
            'enabled': False,
            'masters': [],
            'expiry_alert_days': 7,
        })

    def save_config(self, config: dict) -> tuple:
        """Validate and persist Salt config. Returns (ok, error_msg)."""
        if not isinstance(config, dict):
            return False, "Configuration must be an object"
        if not isinstance(config.get('masters', []), list):
            return False, "'masters' must be a list"
        for m in config.get('masters', []):
            for field in ('id', 'host', 'port', 'username', 'password'):
                if not m.get(field):
                    return False, f"Master '{m.get('id', '?')}': '{field}' is required"

        def _mutate(s):
            s['salt'] = config

        self.settings_manager.update(_mutate, "salt_config_save")
        return True, None

    # ------------------------------------------------------------------ #
    #  salt-api communication                                              #
    # ------------------------------------------------------------------ #

    def _get_token(self, master: dict) -> str:
        """Return a valid token for *master*, re-authenticating if needed."""
        mid = master['id']
        with self._lock:
            cached = self._tokens.get(mid)
            if cached and (time.time() - cached['ts']) < _TOKEN_TTL:
                return cached['token']

        token = self._login(master)
        with self._lock:
            self._tokens[mid] = {'token': token, 'ts': time.time()}
        return token

    def _login(self, master: dict) -> str:
        url = f"http://{master['host']}:{master['port']}/login"
        resp = requests.post(
            url,
            json={
                'username': master['username'],
                'password': master['password'],
                'eauth': master.get('eauth', 'pam'),
            },
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()['return'][0]['token']

    def _run_state(self, master: dict, token: str,
                   minions: list, domain: str,
                   service: str, certmate_url: str, certmate_token: str,
                   deploy_path: str = '') -> dict:
        url = f"http://{master['host']}:{master['port']}"
        pillar = {
            'certmate_domain': domain,
            'certmate_url': certmate_url,
            'certmate_token': certmate_token,
            'service_restart': service,
        }
        if deploy_path:
            pillar['deploy_path'] = deploy_path
        resp = requests.post(
            url,
            json=[{
                'client': 'local',
                'tgt': minions,
                'tgt_type': 'list',
                'fun': 'state.apply',
                'arg': ['certmate.deploy_cert'],
                'kwarg': {'pillar': pillar},
            }],
            headers={'X-Auth-Token': token, 'Content-Type': 'application/json'},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    #  Salt metadata helpers                                               #
    # ------------------------------------------------------------------ #

    def _load_salt_metadata(self, domain: str) -> Optional[dict]:
        path = self.cert_dir / domain / 'salt_metadata.json'
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return None

    def _resolve_master(self, master_id: str) -> Optional[dict]:
        cfg = self.get_config()
        for m in cfg.get('masters', []):
            if m['id'] == master_id:
                return m
        return None

    def _certmate_url_for_minion(self) -> str:
        """Return the CertMate URL that minions can reach.

        Reads CERTMATE_MINION_URL env var first (set in Docker via -e),
        falls back to Salt config, then to localhost.
        """
        import os
        env = os.environ.get('CERTMATE_MINION_URL', '').strip()
        if env:
            return env
        cfg = self.get_config()
        return cfg.get('certmate_minion_url', 'http://localhost:8000')

    def _certmate_api_token(self) -> str:
        """Return the CertMate API token for the Salt state download call."""
        import os
        env = os.environ.get('CERTMATE_API_TOKEN', '').strip()
        if env:
            return env
        cfg = self.get_config()
        return cfg.get('certmate_api_token', '')

    # ------------------------------------------------------------------ #
    #  Deploy                                                              #
    # ------------------------------------------------------------------ #

    def deploy_cert(self, domain: str) -> dict:
        """Deploy *domain* certificate to all configured Salt minions.

        Returns a summary dict with per-master results.
        """
        meta = self._load_salt_metadata(domain)
        if not meta:
            logger.debug(f"Salt deploy skipped for {domain}: no salt_metadata.json")
            return {'skipped': 'no salt metadata'}

        if not meta.get('deploy_enabled', True):
            logger.info(f"Salt deploy disabled in metadata for {domain}")
            return {'skipped': 'deploy_enabled is false'}

        minions = meta.get('minions', [])
        if not minions:
            logger.warning(f"Salt deploy for {domain}: no minions configured")
            return {'skipped': 'no minions'}

        service = meta.get('service_restart', 'nginx')
        deploy_path = meta.get('deploy_path', '')
        certmate_url = self._certmate_url_for_minion()
        certmate_token = self._certmate_api_token()
        results = {}

        for master_id in meta.get('salt_masters', []):
            master = self._resolve_master(master_id)
            if not master:
                logger.warning(f"Salt master '{master_id}' not found in config")
                results[master_id] = {'error': 'master not configured'}
                continue

            try:
                token = self._get_token(master)
                raw = self._run_state(master, token, minions, domain, service,
                                      certmate_url, certmate_token, deploy_path)
                minion_results = raw.get('return', [{}])[0]
                ok_count = 0
                fail_count = 0
                for minion, states in minion_results.items():
                    if isinstance(states, dict):
                        failed = [k for k, v in states.items()
                                  if isinstance(v, dict) and not v.get('result', True)]
                        if failed:
                            fail_count += 1
                            logger.error(f"Salt deploy {domain} → {minion}: {len(failed)} state(s) failed")
                        else:
                            ok_count += 1
                            logger.info(f"Salt deploy {domain} → {minion}: OK")
                    else:
                        logger.warning(f"Salt deploy {domain} → {minion}: unexpected response")
                        fail_count += 1

                results[master_id] = {
                    'ok': fail_count == 0,
                    'minions_ok': ok_count,
                    'minions_failed': fail_count,
                }

                # Invalidate token on partial failure (might have expired)
                if fail_count and ok_count == 0:
                    with self._lock:
                        self._tokens.pop(master_id, None)

            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 401:
                    # Token expired — clear cache and retry once
                    with self._lock:
                        self._tokens.pop(master_id, None)
                    try:
                        token = self._get_token(master)
                        raw = self._run_state(master, token, minions, domain, service,
                                              certmate_url, certmate_token, deploy_path)
                        results[master_id] = {'ok': True, 'retry': True}
                    except Exception as retry_err:
                        logger.error(f"Salt deploy retry failed for {master_id}: {retry_err}")
                        results[master_id] = {'ok': False, 'error': str(retry_err)}
                else:
                    logger.error(f"Salt deploy HTTP error for {master_id}: {e}")
                    results[master_id] = {'ok': False, 'error': str(e)}
            except Exception as e:
                logger.error(f"Salt deploy failed for {master_id}: {e}")
                results[master_id] = {'ok': False, 'error': str(e)}

        return results

    # ------------------------------------------------------------------ #
    #  Remove                                                              #
    # ------------------------------------------------------------------ #

    def _run_remove_state(self, master: dict, token: str,
                          minions: list, domain: str,
                          service: str, deploy_path: str = '') -> dict:
        url = f"http://{master['host']}:{master['port']}"
        pillar = {'certmate_domain': domain, 'service_restart': service}
        if deploy_path:
            pillar['deploy_path'] = deploy_path
        resp = requests.post(
            url,
            json=[{
                'client': 'local',
                'tgt': minions,
                'tgt_type': 'list',
                'fun': 'state.apply',
                'arg': ['certmate.remove_cert'],
                'kwarg': {'pillar': pillar},
            }],
            headers={'X-Auth-Token': token, 'Content-Type': 'application/json'},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def remove_cert(self, domain: str) -> dict:
        """Remove certificate files from minions. Call BEFORE deleting the cert in CertMate."""
        meta = self._load_salt_metadata(domain)
        if not meta:
            return {'skipped': 'no salt metadata'}

        minions = meta.get('minions', [])
        if not minions:
            return {'skipped': 'no minions'}

        service = meta.get('service_restart', 'nginx')
        deploy_path = meta.get('deploy_path', '')
        results = {}

        for master_id in meta.get('salt_masters', []):
            master = self._resolve_master(master_id)
            if not master:
                results[master_id] = {'error': 'master not configured'}
                continue
            try:
                token = self._get_token(master)
                raw = self._run_remove_state(master, token, minions, domain, service, deploy_path)
                minion_results = raw.get('return', [{}])[0]
                ok_count = sum(1 for s in minion_results.values()
                               if isinstance(s, dict) and
                               all(v.get('result', True) for v in s.values() if isinstance(v, dict)))
                fail_count = len(minion_results) - ok_count
                results[master_id] = {'ok': fail_count == 0, 'minions_ok': ok_count, 'minions_failed': fail_count}
                logger.info(f"Salt remove {domain} via {master_id}: ok={ok_count} failed={fail_count}")
            except Exception as e:
                logger.error(f"Salt remove failed for {master_id}: {e}")
                results[master_id] = {'ok': False, 'error': str(e)}

        return results

    # ------------------------------------------------------------------ #
    #  Event bus listener                                                  #
    # ------------------------------------------------------------------ #

    def on_certificate_event(self, event: str, data: dict):
        """Called by EventBus on certificate_renewed / certificate_created."""
        if event not in ('certificate_renewed', 'certificate_created'):
            return
        cfg = self.get_config()
        if not cfg.get('enabled'):
            return
        domain = data.get('domain')
        if not domain:
            return
        try:
            results = self.deploy_cert(domain)
            logger.info(f"Salt deploy triggered for {domain}: {results}")
        except Exception as e:
            logger.error(f"Salt deploy event handler error for {domain}: {e}")

    # ------------------------------------------------------------------ #
    #  Connection test                                                     #
    # ------------------------------------------------------------------ #

    def test_master(self, master: dict) -> dict:
        """Test connectivity and authentication to a Salt master."""
        try:
            token = self._login(master)
            url = f"http://{master['host']}:{master['port']}"
            resp = requests.post(
                url,
                json=[{'client': 'local', 'tgt': '*', 'fun': 'test.ping'}],
                headers={'X-Auth-Token': token, 'Content-Type': 'application/json'},
                timeout=10,
            )
            resp.raise_for_status()
            minions = list(resp.json().get('return', [{}])[0].keys())
            return {'ok': True, 'minions': minions, 'count': len(minions)}
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 401:
                return {'ok': False, 'error': 'Authentication failed — check username/password'}
            return {'ok': False, 'error': f'HTTP {status}: {e}'}
        except requests.ConnectionError:
            return {'ok': False, 'error': f"Cannot connect to {master['host']}:{master['port']}"}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ------------------------------------------------------------------ #
    #  Expiry alert                                                        #
    # ------------------------------------------------------------------ #

    def check_expiry_alerts(self):
        """Scan all certs and send notification for those expiring soon.

        Called daily by APScheduler. Uses the threshold from Salt config
        (default 7 days) and sends via the existing Notifier if SMTP is on.
        """
        from .notifier import Notifier
        cfg = self.get_config()
        threshold = int(cfg.get('expiry_alert_days', 7))
        settings = self.settings_manager.load_settings()
        notifier = Notifier(self.settings_manager)

        expiring = []
        for domain_entry in settings.get('domains', []):
            domain = domain_entry if isinstance(domain_entry, str) else domain_entry.get('domain', '')
            if not domain:
                continue
            cert_path = self.cert_dir / domain / 'cert.pem'
            if not cert_path.exists():
                continue
            try:
                from cryptography import x509
                from cryptography.hazmat.backends import default_backend
                import datetime
                cert = x509.load_pem_x509_certificate(
                    cert_path.read_bytes(), default_backend()
                )
                now = datetime.datetime.now(datetime.timezone.utc)
                days_left = (cert.not_valid_after_utc - now).days
                if 0 < days_left <= threshold:
                    expiring.append({'domain': domain, 'days_left': days_left})
            except Exception as e:
                logger.warning(f"Expiry check failed for {domain}: {e}")

        if not expiring:
            logger.info(f"Expiry alert check: no certificates expiring within {threshold} days")
            return

        expiring.sort(key=lambda x: x['days_left'])
        lines = '\n'.join(
            f"  • {e['domain']} — expires in {e['days_left']} day(s)" for e in expiring
        )
        title = f"⚠️ {len(expiring)} Certificate(s) Expiring Within {threshold} Days"
        message = (
            f"The following certificates expire within {threshold} days "
            f"and may not renew automatically:\n\n{lines}\n\n"
            f"Log into CertMate to renew them manually if needed."
        )
        logger.warning(f"Expiry alert: {len(expiring)} cert(s) — {[e['domain'] for e in expiring]}")
        notifier.notify('certificate_expiring', title, message,
                        details={'expiring': expiring, 'threshold_days': threshold})
