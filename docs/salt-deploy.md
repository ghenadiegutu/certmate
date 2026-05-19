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

## Salt States

Posiziona entrambi i file su ogni Salt Master in `/srv/salt/certmate/`.

### `deploy_cert.sls` — Installa il certificato

> Scarica direttamente sul **minion** (non passa per il master). Richiede `curl` e `python3` (sempre disponibili su OEL/RHEL/Ubuntu). **Non richiede `unzip`.**

```jinja
{% set domain       = pillar.get('certmate_domain', '') %}
{% set certmate_url = pillar.get('certmate_url', 'http://localhost:8000') %}
{% set token        = pillar.get('certmate_token', '') %}
{% set service      = pillar.get('service_restart', 'nginx') %}
{% set cert_dir     = pillar.get('deploy_path', '/etc/nginx/ssl/' ~ domain) %}
{% set zip_tmp      = '/tmp/certmate_' ~ domain ~ '.zip' %}
{% set restart_cmd  = pillar.get('restart_cmd', '') %}

{{ cert_dir }}:
  file.directory:
    - makedirs: True
    - mode: 700

download_cert_zip_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: >
        curl -sf -H "Authorization: Bearer {{ token }}"
        "{{ certmate_url }}/api/certificates/{{ domain }}/download"
        -o {{ zip_tmp }}
    - require:
      - file: {{ cert_dir }}

extract_cert_zip_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: python3 -c "import zipfile; zipfile.ZipFile('{{ zip_tmp }}').extractall('{{ cert_dir }}')"
    - require:
      - cmd: download_cert_zip_{{ domain | replace('.', '_') }}

rename_cert_files_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: >
        mv -f {{ cert_dir }}/fullchain.pem {{ cert_dir }}/fullchain.cer &&
        mv -f {{ cert_dir }}/privkey.pem {{ cert_dir }}/{{ domain }}.key
    - require:
      - cmd: extract_cert_zip_{{ domain | replace('.', '_') }}

{{ cert_dir }}/{{ domain }}.key:
  file.managed:
    - mode: 600
    - replace: False
    - require:
      - cmd: rename_cert_files_{{ domain | replace('.', '_') }}

cleanup_zip_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: rm -f {{ zip_tmp }}
    - require:
      - cmd: rename_cert_files_{{ domain | replace('.', '_') }}

{% if restart_cmd %}
reload_service_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: {{ restart_cmd }}
    - require:
      - cmd: rename_cert_files_{{ domain | replace('.', '_') }}
{% else %}
{{ service }}_reload_{{ domain | replace('.', '_') }}:
  service.running:
    - name: {{ service }}
    - reload: True
    - watch:
      - cmd: rename_cert_files_{{ domain | replace('.', '_') }}
{% endif %}
```

**File risultanti sul minion:**
| File | Descrizione |
|---|---|
| `fullchain.cer` | Certificato + chain (rinominato da `fullchain.pem`) |
| `<domain>.key` | Chiave privata (rinominata da `privkey.pem`) — chmod 600 |
| `cert.pem` | Solo certificato |
| `chain.pem` | Solo chain intermedia |

### `remove_cert.sls` — Rimuove il certificato

Usata da CertMate quando si cancella un certificato dalla dashboard (con la checkbox "Rimuovi anche dai server Salt").

```jinja
{% set domain   = pillar.get('certmate_domain', '') %}
{% set cert_dir = pillar.get('deploy_path', '/etc/nginx/ssl/' ~ domain) %}
{% set service  = pillar.get('service_restart', 'nginx') %}

remove_cert_dir_{{ domain | replace('.', '_') }}:
  file.absent:
    - name: {{ cert_dir }}

{{ service }}_reload_after_remove_{{ domain | replace('.', '_') }}:
  service.running:
    - name: {{ service }}
    - reload: True
    - require:
      - file: remove_cert_dir_{{ domain | replace('.', '_') }}
```

---

## Salt Metadata per certificato

Ogni certificato può avere un file `salt_metadata.json` nella sua directory (`certificates/<domain>/`). Configurabile dalla dashboard nel pannello dettaglio del certificato.

```json
{
  "cert_name": "example.com",
  "salt_masters": ["salt-master-prod"],
  "minions": ["web-01", "web-02"],
  "service_restart": "nginx",
  "deploy_path": "/etc/nginx/ssl/example.com",
  "environment": "production",
  "deploy_enabled": true
}
```

| Campo | Default | Descrizione |
|---|---|---|
| `salt_masters` | `[]` | ID dei Salt Master configurati in Settings → Salt |
| `minions` | `[]` | Lista minion target (nomi esatti Salt) |
| `service_restart` | `nginx` | Servizio systemd da ricaricare dopo il deploy |
| `deploy_path` | `/etc/nginx/ssl/<domain>` | Path destinazione sul minion |
| `restart_cmd` | `` | Comando custom per riavvio (es. Docker Compose) — se impostato, sovrascrive `service_restart` |
| `deploy_enabled` | `true` | Disabilita il deploy automatico per questo cert |

### API

```bash
# Salva metadati
curl -X POST http://localhost:8000/api/web/certificates/example.com/salt-metadata \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "salt_masters": ["salt-master-prod"],
    "minions": ["web-01", "web-02"],
    "service_restart": "nginx",
    "deploy_path": "/etc/nginx/ssl/example.com",
    "deploy_enabled": true
  }'

# Deploy manuale
curl -X POST http://localhost:8000/api/salt/deploy/example.com \
  -H "Authorization: Bearer <token>"

# Rimuovi dai minion (prima di cancellare il cert)
curl -X POST http://localhost:8000/api/salt/remove/example.com \
  -H "Authorization: Bearer <token>"
```

---

## Cancellazione certificato con cleanup Salt

Quando si cancella un certificato dalla dashboard, se il cert ha Salt metadata configurata appare una checkbox:

> **Rimuovi anche dai server Salt (N minions: web-01, web-02)**

- **Spuntata (default)**: CertMate chiama prima `POST /api/salt/remove/<domain>` (rimuove la directory `deploy_path` su tutti i minion e ricarica il servizio), poi cancella il cert da CertMate.
- **Deselezionata**: cancella solo da CertMate, i file rimangono sui server.

> **Nota**: il cleanup avviene *prima* della cancellazione del cert, così `salt_metadata.json` è ancora disponibile per determinare quali minion e quale path pulire.

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

## HTTP-01 Challenge con Varnish

CertMate supporta la validazione **HTTP-01** di Let's Encrypt tramite la modalità webroot di certbot. Nessuna porta aggiuntiva richiesta — i token vengono serviti sulla stessa porta di CertMate (8000 o quella configurata).

### Flusso

```
Let's Encrypt → GET http://domain.com/.well-known/acme-challenge/<token>
                         ↓
Varnish → proxy a CertMate:8000
                         ↓
CertMate serve il token da /app/data/acme-challenges/
```

### Configurazione Varnish (VCL)

Aggiungi su ogni Varnish che gestisce domini con certificati CertMate:

```vcl
backend certmate {
    .host = "192.168.1.X";   # IP di CertMate
    .port = "8000";
}

sub vcl_recv {
    if (req.url ~ "^/.well-known/acme-challenge/") {
        set req.backend_hint = certmate;
        return(pass);
    }
}
```

### Verifica

```bash
# Dal server Varnish — deve rispondere 404 (CertMate raggiunto, token non esiste)
curl -v http://tuo-dominio/.well-known/acme-challenge/test
# Se risponde la pagina del tuo sito → il VCL non è applicato
```

### Creazione certificato

In CertMate → Create Certificate → **Challenge Type: HTTP-01**

> HTTP-01 non supporta wildcard (`*.domain.com`) — per quelli usa DNS-01.

---

## Docker Compose sui minion

Se il servizio gira come container Docker invece di systemd, usa il campo **Restart Command** nei metadata Salt:

```json
{
  "restart_cmd": "docker compose -f /opt/myapp/docker-compose.yml restart web"
}
```

Il `docker-compose.yml` del minion deve montare la directory dei certificati:

```yaml
services:
  web:
    volumes:
      - /etc/nginx/ssl:/etc/nginx/ssl:ro
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

### 5 stati falliti sul minion
Causa più comune: il minion non raggiunge CertMate. Verifica che **CertMate URL** in Settings → Salt sia l'IP raggiungibile **dal minion** con la porta corretta (es. `http://192.168.1.X:8000`).

```bash
# Testa dal minion
salt '<minion>' cmd.run "curl -sv http://<certmate-ip>:8000/api/health"
```

### `unzip: command not found`
La state usa `python3` (disponibile su OEL/RHEL senza installare nulla). Se hai una versione vecchia della state, aggiornala:
```bash
curl -o /srv/salt/certmate/deploy_cert.sls \
  https://raw.githubusercontent.com/ghenadiegutu/certmate/main/scripts/salt_states/certmate/deploy_cert.sls
```

### Log deploy
```bash
# Sul container CertMate
docker logs certmate 2>&1 | grep -i salt
```
