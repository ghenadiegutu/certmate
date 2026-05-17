# Salt Deploy — Guida Completa

CertMate integra nativamente Salt Stack per distribuire automaticamente i certificati SSL ai server web dopo ogni rinnovo o creazione.

---

## Indice

- [Architettura](#architettura)
- [Flusso completo](#flusso-completo)
- [Configurazione CertMate](#configurazione-certmate)
- [Configurazione Salt Master](#configurazione-salt-master)
- [Salt State](#salt-state)
- [Salt Metadata per certificato](#salt-metadata-per-certificato)
- [Alert di scadenza email](#alert-di-scadenza-email)
- [Setup ambiente di test con LXC](#setup-ambiente-di-test-con-lxc)
- [Troubleshooting](#troubleshooting)

---

## Architettura

```
┌─────────────────────────────────────────────────────────────────┐
│                        PRODUZIONE                               │
│                                                                 │
│  ┌────────────────────────────────┐                             │
│  │   CertMate VM (Docker)         │                             │
│  │   - SaltManager (integrato)    │──────────────────────────┐  │
│  │   - EventBus listener          │  salt-api HTTP call      │  │
│  │   - APScheduler (expiry alert) │                          ▼  │
│  └────────────────────────────────┘                             │
│                                      ┌──────────────────────────┤
│                                      │  Salt Master VM          │
│                                      │  - salt-master           │
│                                      │  - salt-api (:8080)      │
│                                      └────────────┬─────────────┤
│                                           state.apply           │
│                                      ┌────────────▼─────────────┤
│                                      │  Minion VMs (N)          │
│                                      │  - nginx / apache2       │
│                                      │  - /etc/ssl/<domain>/    │
│                                      └──────────────────────────┘
└─────────────────────────────────────────────────────────────────┘
```

**Nessun relay esterno richiesto.** La VM CertMate chiama direttamente l'API REST del Salt Master.

---

## Flusso completo

```
PASSO 1 — Certificato rinnovato o creato
  └─ APScheduler (02:00) rinnova il certificato
  └─ EventBus emette: certificate_renewed / certificate_created

PASSO 2 — SaltManager riceve l'evento
  └─ Legge salt_metadata.json dalla directory del certificato
  └─ Verifica che deploy_enabled = true e che ci siano minion

PASSO 3 — Autenticazione su salt-api
  └─ POST http://<salt-master>:8080/login
  └─ Token in cache per 1 ora, rinnovo automatico

PASSO 4 — Esecuzione state.apply
  └─ POST http://<salt-master>:8080
  └─ client: local, tgt: ["web-01", "web-02"], fun: state.apply
  └─ Pillar: dominio, URL CertMate, token, servizio da riavviare

PASSO 5 — Minion esegue certmate.deploy_cert
  └─ curl → GET /api/certificates/<domain>/download (da CertMate)
  └─ unzip → /etc/nginx/ssl/<domain>/
  └─ chmod 600 privkey.pem
  └─ systemctl reload nginx

PASSO 6 — Risultato
  └─ SaltManager riceve esito per ogni minion
  └─ Log in CertMate: ok/failed per minion
```

---

## Configurazione CertMate

Vai su **Settings → Salt** nella UI di CertMate.

| Campo | Descrizione |
|---|---|
| **Enable Salt Deploy** | Attiva/disattiva il deploy automatico globale |
| **Expiry Alert Threshold** | Giorni prima della scadenza per inviare alert email (default: 7) |
| **CertMate URL (for minions)** | URL raggiungibile dai minion per scaricare i cert (es. `http://192.168.1.100:8000`) |
| **CertMate API Token** | Token con ruolo `viewer` per autenticare i download dei minion |

### Salt Masters

Per ogni master configura:

| Campo | Esempio |
|---|---|
| **ID** | `salt-master-prod` |
| **Label** | `Production Master` |
| **Host** | `10.46.138.223` |
| **Port** | `8080` |
| **Username** | `saltapi` |
| **Password** | `***` |
| **Auth Method** | `pam` |
| **Environment** | `production` |

Usa il pulsante **Test** per verificare la connettività prima di salvare.

---

## Configurazione Salt Master

### 1. Installa salt-api

```bash
apt install salt-api
```

### 2. `/etc/salt/master.d/api.conf`

```yaml
rest_cherrypy:
  port: 8080
  disable_ssl: true

netapi_enable_clients:
  - local
  - runner
  - wheel
```

> **Nota Salt 3008+**: `netapi_enable_clients` è obbligatorio, altrimenti ricevi `Client disabled: local`.

### 3. Utente saltapi con accesso limitato

```bash
useradd -r -s /bin/bash saltapi
echo "saltapi:PASSWORD" | chpasswd
usermod -a -G shadow salt   # necessario per PAM in alcuni ambienti
```

### 4. `/etc/salt/master.d/acl.conf` — ACL limitata

```yaml
external_auth:
  pam:
    saltapi:
      - '*':
          - state.apply
          - test.ping
```

L'utente `saltapi` può solo applicare stati e fare ping — nessun accesso shell o comandi di sistema.

### 5. Riavvia i servizi

```bash
systemctl restart salt-master salt-api
```

---

## Salt State

Posiziona questo file su ogni Salt Master in `/srv/salt/certmate/deploy_cert.sls`:

```yaml
# Scarica e installa il certificato da CertMate
{% set domain = pillar.get('certmate_domain', '') %}
{% set certmate_url = pillar.get('certmate_url', 'http://localhost:8000') %}
{% set certmate_token = pillar.get('certmate_token', '') %}
{% set service = pillar.get('service_restart', 'nginx') %}
{% set cert_dir = '/etc/ssl/certs/' + domain %}

create_cert_dir:
  file.directory:
    - name: {{ cert_dir }}
    - makedirs: True
    - mode: '0755'

download_cert_zip:
  cmd.run:
    - name: >
        curl -fsSL -H "Authorization: Bearer {{ certmate_token }}"
        "{{ certmate_url }}/api/certificates/{{ domain }}/download"
        -o /tmp/cert_{{ domain }}.zip
    - require:
      - file: create_cert_dir

extract_cert:
  cmd.run:
    - name: unzip -o /tmp/cert_{{ domain }}.zip -d {{ cert_dir }}
    - require:
      - cmd: download_cert_zip

set_key_permissions:
  file.managed:
    - name: {{ cert_dir }}/privkey.pem
    - mode: '0600'
    - require:
      - cmd: extract_cert

reload_service:
  service.running:
    - name: {{ service }}
    - reload: True
    - require:
      - file: set_key_permissions
```

---

## Salt Metadata per certificato

Ogni certificato può avere un file `salt_metadata.json` nella sua directory (`certificates/<domain>/`). Questo file indica a CertMate dove deployare.

```json
{
  "cert_name": "example.com",
  "salt_masters": ["salt-master-prod"],
  "minions": ["web-01", "web-02"],
  "service_restart": "nginx",
  "environment": "production",
  "deploy_enabled": true
}
```

Il file viene creato/aggiornato tramite l'API:

```bash
# Salva metadati
curl -X POST http://localhost:8000/api/web/certificates/example.com/salt-metadata \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"salt_masters":["salt-master-prod"],"minions":["web-01"],"service_restart":"nginx","deploy_enabled":true}'

# Trigger deploy manuale
curl -X POST http://localhost:8000/api/salt/deploy/example.com \
  -H "Authorization: Bearer <token>"
```

---

## Alert di scadenza email

SaltManager esegue un controllo giornaliero alle **08:00** tramite APScheduler.

- Legge la soglia `expiry_alert_days` dalla configurazione Salt (default: 7 giorni)
- Scansiona tutti i certificati in `settings.json → domains`
- Invia una notifica email tramite il sistema SMTP di CertMate per ogni certificato che scade entro la soglia

Per ricevere gli alert è necessario:
1. Avere SMTP configurato in **Settings → Notifications**
2. Avere Salt abilitato con una soglia impostata

---

## Setup ambiente di test con LXC

### Prerequisiti

```bash
sudo apt install lxd
sudo lxd init --minimal
```

### Crea i container

```bash
# Salt Master
lxc launch ubuntu:22.04 salt-master
lxc exec salt-master -- bash -c "
  apt update && apt install -y curl gnupg2
  curl -fsSL https://packages.broadcom.com/artifactory/api/security/keypair/SaltProjectKey/public \
    | gpg --dearmor -o /usr/share/keyrings/salt-archive-keyring.gpg
  echo 'deb [signed-by=/usr/share/keyrings/salt-archive-keyring.gpg] \
    https://packages.broadcom.com/artifactory/saltproject-deb/ stable main' \
    > /etc/apt/sources.list.d/salt.list
  apt update && apt install -y salt-master salt-api
"

# Minion web-01
lxc launch ubuntu:22.04 web-01
lxc exec web-01 -- bash -c "
  # (stesso setup repo)
  apt install -y salt-minion nginx curl unzip
"
```

### Configura il minion

```bash
lxc exec web-01 -- bash -c "
  echo 'master: <IP-SALT-MASTER>' > /etc/salt/minion
  systemctl restart salt-minion
"

# Accetta la chiave sul master
lxc exec salt-master -- salt-key -A -y
```

### Verifica

```bash
lxc exec salt-master -- salt '*' test.ping
# Expected: web-01: True
```

---

## Troubleshooting

### `Client disabled: local` (Salt 3008+)
Aggiungi a `/etc/salt/master.d/api.conf`:
```yaml
netapi_enable_clients:
  - local
  - runner
  - wheel
```

### `401 Unauthorized` dalla salt-api
- Verifica che l'utente `saltapi` abbia shell `/bin/bash`
- In LXC: aggiungi `salt` al gruppo `shadow` — `usermod -a -G shadow salt`
- Riavvia salt-master e salt-api dopo ogni modifica

### Minion non risponde
```bash
salt-master -- salt-key -L   # verifica che la chiave sia accettata
salt-master -- salt 'web-01' test.ping
```

### Test connettività dalla UI
Usa il pulsante **Test** su ogni Salt Master in Settings → Salt. Mostra il numero di minion visibili.

### Log deploy
```bash
# Sul container CertMate
docker logs certmate 2>&1 | grep -i salt
```
