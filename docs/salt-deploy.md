# Salt Deploy — Guida Completa

Questo documento descrive come CertMate distribuisce automaticamente i certificati SSL sui server tramite **Salt Stack**, partendo dalla creazione del certificato fino al riavvio del servizio web sul minion.

---

## Indice

- [Architettura](#architettura)
- [Componenti](#componenti)
- [Flusso completo](#flusso-completo)
- [File e cartelle chiave](#file-e-cartelle-chiave)
- [Configurazione](#configurazione)
- [Setup ambiente di test con LXC](#setup-ambiente-di-test-con-lxc)
- [Avvio dei servizi](#avvio-dei-servizi)
- [Test manuale](#test-manuale)
- [Troubleshooting](#troubleshooting)

---

## Architettura

```
┌────────────────────────────────────────────────────────────────┐
│                        TUO PC / SERVER                         │
│                                                                │
│  ┌─────────────────────┐        ┌──────────────────────────┐  │
│  │   CertMate          │        │  salt_webhook_server.py  │  │
│  │   (Docker :8000)    │──POST──▶  (Host :5001)            │  │
│  │                     │        │                          │  │
│  │  - Gestisce cert    │        │  - Riceve notifiche      │  │
│  │  - Salva metadata   │        │  - Chiama salt_deploy.py │  │
│  │  - Deploy hooks     │        └──────────┬───────────────┘  │
│  └─────────────────────┘                   │                  │
│                                      Salt API call            │
│  ┌─────────────────────────────────────────▼───────────────┐  │
│  │  LXC: salt-master (10.46.138.223)                       │  │
│  │  - Salt Master                                          │  │
│  │  - salt-api CherryPy (:8080)                            │  │
│  └─────────────────────────────────────────┬───────────────┘  │
│                                     state.apply               │
│  ┌─────────────────────────────────────────▼───────────────┐  │
│  │  LXC: web-01 (10.46.138.7)                              │  │
│  │  - Salt Minion                                          │  │
│  │  - nginx                                                │  │
│  │  - Riceve il cert → /etc/nginx/ssl/<domain>/            │  │
│  └─────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

> **Nota reti:** Docker e LXC usano bridge separati (`docker0` e `lxdbr0`), per questo serve il `salt_webhook_server.py` sull'host come relay.

---

## Componenti

### 1. CertMate (Docker)
- Gestisce il ciclo di vita dei certificati SSL
- Salva i **Salt metadata** per ogni certificato (`salt_metadata.json`)
- Quando un cert viene rinnovato/creato, esegue i **deploy hooks** configurati
- Espone il webhook endpoint: `POST /api/webhook/cert-renewed`

### 2. salt_webhook_server.py (Host)
- Piccolo HTTP server Python (porta 5001)
- Ascolta su `0.0.0.0:5001` — raggiungibile da Docker via `172.17.0.1:5001`
- Riceve `POST /deploy` con il dominio
- Chiama `salt_deploy.py` in un thread separato (risposta immediata 202)

### 3. salt_deploy.py (Host)
- Script Python che orchestra il deploy Salt
- Legge i metadata dal webhook di CertMate
- Fa login su salt-api
- Chiama `state.apply` sui minion configurati

### 4. Salt Master + salt-api (LXC)
- Salt Master che gestisce i minion
- salt-api (CherryPy) espone REST API sulla porta 8080
- Autenticazione PAM con utente `saltapi`

### 5. Salt State: `certmate.deploy_cert` (sul Master)
- Scarica il certificato ZIP da CertMate
- Estrae i file in `/etc/nginx/ssl/<domain>/`
- Imposta i permessi corretti sulla chiave privata
- Esegue reload del servizio configurato (nginx, apache2, ecc.)

### 6. Minion (LXC web-01)
- Riceve il comando dal master
- Esegue lo state (download cert + reload nginx)
- Risultato riportato al master

---

## Flusso completo

```
PASSO 1 — Utente configura il certificato in CertMate UI
  └─ Compila sezione "Salt Deploy Configuration":
       • Salt Master(s): salt-master-prod
       • Minion Targets: web-01, web-02
       • Ambiente: production
       • Servizio: nginx
       • Auto-deploy: ✓

PASSO 2 — CertMate salva salt_metadata.json
  └─ File: certificates/<domain>/salt_metadata.json
  └─ Contenuto:
     {
       "cert_name": "example.com",
       "salt_masters": ["salt-master-prod"],
       "minions": ["web-01", "web-02"],
       "service_restart": "nginx",
       "environment": "production",
       "deploy_enabled": true
     }

PASSO 3 — Cert rinnovato (o creato)
  └─ APScheduler alle 02:00 controlla le scadenze
  └─ Esegue certbot per rinnovare
  └─ Triggera evento: certificate_renewed

PASSO 4 — Deploy hook eseguito da CertMate
  └─ Comando: /app/scripts/hook_salt_deploy.sh
  └─ Variabili iniettate:
       CERTMATE_DOMAIN=example.com
       CERTMATE_CERT_PATH=/app/certificates/example.com/cert.pem
       CERTMATE_KEY_PATH=/app/certificates/example.com/privkey.pem
       CERTMATE_EVENT=certificate_renewed

PASSO 5 — hook_salt_deploy.sh → webhook server
  └─ POST http://172.17.0.1:5001/deploy
  └─ Body: domain=example.com
  └─ Risposta immediata: {"status":"accepted"}

PASSO 6 — salt_webhook_server.py riceve e delega
  └─ Avvia thread: salt_deploy.py example.com

PASSO 7 — salt_deploy.py legge i metadata
  └─ POST http://localhost:8000/api/webhook/cert-renewed
  └─ Body: {"cert_name": "example.com"}
  └─ Risposta: salt_metadata.json completo

PASSO 8 — salt_deploy.py si autentica su salt-api
  └─ POST http://10.46.138.223:8080/login
  └─ Ottiene token

PASSO 9 — salt_deploy.py esegue state.apply
  └─ POST http://10.46.138.223:8080
  └─ client: local, tgt: ["web-01"], fun: state.apply
  └─ Pillar: dominio, URL CertMate, token, servizio

PASSO 10 — Salt Master invia il comando al minion
  └─ web-01 esegue certmate.deploy_cert state

PASSO 11 — Minion scarica e deploya il certificato
  └─ curl → GET /api/certificates/example.com/download
  └─ unzip → /etc/nginx/ssl/example.com/
  └─ chmod 600 privkey.pem
  └─ systemctl reload nginx

PASSO 12 — Risultato riportato al master e loggato
  └─ salt_deploy.py logga: "web-01: OK (5 cambiamenti)"
```

---

## File e cartelle chiave

```
certmate/
├── certificates/
│   └── <domain>/
│       ├── cert.pem
│       ├── chain.pem
│       ├── fullchain.pem
│       ├── privkey.pem
│       └── salt_metadata.json          ← metadata Salt per questo cert
│
├── config/
│   └── salt_masters.json               ← elenco Salt Masters configurati
│
└── scripts/
    ├── salt_deploy.py                  ← script deploy (gira sull'host)
    ├── salt_webhook_server.py          ← relay server (gira sull'host)
    ├── hook_salt_deploy.sh             ← chiamato da CertMate deploy hook
    └── salt_states/
        └── certmate/
            └── deploy_cert.sls         ← Salt state (sul master)
```

**Sul Salt Master (LXC):**
```
/srv/salt/
└── certmate/
    └── deploy_cert.sls                 ← copiato da scripts/salt_states/
```

**Sul Minion dopo il deploy:**
```
/etc/nginx/ssl/
└── <domain>/
    ├── cert.pem
    ├── chain.pem
    ├── fullchain.pem
    └── privkey.pem  (chmod 600)
```

---

## Configurazione

### config/salt_masters.json

Elenco dei Salt Master disponibili. Ogni master ha:

```json
{
  "masters": [
    {
      "id": "salt-master-prod",        // ID usato nei metadata
      "label": "salt-master-prod (Production)",
      "host": "10.46.138.223",         // IP raggiungibile dall'host
      "port": 8080,                    // Porta salt-api
      "environment": "production"
    }
  ],
  "environments": ["production", "staging", "development"],
  "services":     ["nginx", "apache2", "httpd", "haproxy", "traefik"]
}
```

### Variabili d'ambiente per salt_deploy.py / salt_webhook_server.py

| Variabile | Descrizione | Default |
|---|---|---|
| `CERTMATE_URL` | URL CertMate raggiungibile dall'host | `http://localhost:8000` |
| `CERTMATE_TOKEN` | API token CertMate (ruolo viewer+) | — |
| `SALT_API_USER` | Utente salt-api | `saltapi` |
| `SALT_API_PASSWORD` | Password salt-api | — |
| `SALT_API_EAUTH` | Metodo auth (pam, ldap, file) | `pam` |
| `WEBHOOK_PORT` | Porta del relay server | `5001` |

### Deploy hook in CertMate (data/settings.json)

```json
"deploy_hooks": {
  "enabled": true,
  "global_hooks": [
    {
      "id": "salt-deploy",
      "name": "Salt Deploy",
      "command": "/app/scripts/hook_salt_deploy.sh",
      "enabled": true,
      "on_events": ["certificate_renewed", "certificate_created"],
      "timeout": 15
    }
  ]
}
```

---

## Setup ambiente di test con LXC

### Prerequisiti

```bash
sudo snap install lxd
sudo lxd init --auto
sudo usermod -aG lxd $USER
newgrp lxd
```

### Crea i container

```bash
lxc launch ubuntu:24.04 salt-master
lxc launch ubuntu:24.04 web-01
```

### Installa Salt Master

```bash
lxc exec salt-master -- bash -c "
  curl -fsSL https://packages.broadcom.com/artifactory/api/security/keypair/SaltProjectKey/public \
    | gpg --dearmor -o /etc/apt/keyrings/salt-archive-keyring.gpg
  echo 'deb [signed-by=/etc/apt/keyrings/salt-archive-keyring.gpg arch=amd64] \
    https://packages.broadcom.com/artifactory/saltproject-deb stable main' \
    > /etc/apt/sources.list.d/salt.list
  apt-get update -q
  apt-get install -y salt-master salt-api
  /opt/saltstack/salt/bin/pip install cherrypy
"
```

### Configura salt-api

```bash
lxc exec salt-master -- bash -c "
  useradd -s /bin/bash saltapi
  echo 'saltapi:SaltApi2026!' | chpasswd
  usermod -a -G shadow salt

  cat > /etc/salt/master.d/api.conf << 'EOF'
external_auth:
  pam:
    saltapi:
      - '.*'
      - '@wheel'
      - '@runner'
rest_cherrypy:
  port: 8080
  disable_ssl: true
netapi_enable_clients:
  - local
  - runner
  - wheel
EOF

  systemctl restart salt-master salt-api
"
```

### Installa Salt Minion + nginx

```bash
MASTER_IP=$(lxc list salt-master --format csv -c 4 | cut -d' ' -f1)

lxc exec web-01 -- bash -c "
  curl -fsSL https://packages.broadcom.com/artifactory/api/security/keypair/SaltProjectKey/public \
    | gpg --dearmor -o /etc/apt/keyrings/salt-archive-keyring.gpg
  echo 'deb [signed-by=/etc/apt/keyrings/salt-archive-keyring.gpg arch=amd64] \
    https://packages.broadcom.com/artifactory/saltproject-deb stable main' \
    > /etc/apt/sources.list.d/salt.list
  apt-get update -q
  apt-get install -y salt-minion nginx unzip

  cat > /etc/salt/minion << EOF
master: $MASTER_IP
id: web-01
EOF
  systemctl restart salt-minion
"

# Accetta la chiave del minion
sleep 10
lxc exec salt-master -- salt-key -A -y
```

### Copia lo Salt state

```bash
lxc exec salt-master -- mkdir -p /srv/salt/certmate
lxc file push scripts/salt_states/certmate/deploy_cert.sls \
    salt-master/srv/salt/certmate/deploy_cert.sls
```

### Verifica

```bash
lxc exec salt-master -- salt 'web-01' test.ping
# web-01: True
```

---

## Avvio dei servizi

```bash
cd /path/to/certmate

# 1. CertMate Docker
docker start certmate-test
# oppure primo avvio:
# docker run -d --name certmate-test -p 8000:8000 \
#   -v $(pwd)/data:/app/data \
#   -v $(pwd)/certificates:/app/certificates \
#   -v $(pwd)/config:/app/config \
#   -v $(pwd)/scripts:/app/scripts \
#   certmate-local:latest

# 2. Salt webhook relay server (host)
source .venv/bin/activate
CERTMATE_URL=http://localhost:8000 \
CERTMATE_TOKEN=<il_tuo_token> \
SALT_API_PASSWORD=SaltApi2026! \
python3 scripts/salt_webhook_server.py &
```

---

## Test manuale

### 1. Verifica che tutto comunichi

```bash
# CertMate health
curl http://localhost:8000/health

# Webhook server health
curl http://localhost:5001/health

# Salt ping
lxc exec salt-master -- salt 'web-01' test.ping
```

### 2. Crea metadata per un dominio

```bash
curl -X POST http://localhost:8000/api/web/certificates/example.com/salt-metadata \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "salt_masters": ["salt-master-prod"],
    "minions": ["web-01"],
    "environment": "production",
    "service_restart": "nginx",
    "deploy_enabled": true
  }'
```

### 3. Simula un deploy (senza aspettare il rinnovo)

```bash
# Via API CertMate (richiede ruolo admin)
curl -X POST http://localhost:8000/api/certificates/example.com/deploy \
  -H "Authorization: Bearer <admin_token>"

# Oppure chiama il webhook direttamente
curl -X POST http://localhost:5001/deploy \
  -d "domain=example.com"
```

### 4. Verifica il risultato sul minion

```bash
lxc exec web-01 -- ls -la /etc/nginx/ssl/example.com/
lxc exec web-01 -- openssl x509 -in /etc/nginx/ssl/example.com/cert.pem -noout -dates
lxc exec web-01 -- systemctl is-active nginx
```

---

## Troubleshooting

### Docker non raggiunge salt-api

Il container Docker e LXC usano bridge di rete separati. Per questo esiste `salt_webhook_server.py` che gira sull'host come intermediario. Se il problema persiste:

```bash
# Fix permanente DNS Docker
echo '{"dns": ["8.8.8.8", "8.8.4.4"]}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

### salt-api restituisce 401

```bash
# Verifica che salt sia nel gruppo shadow
lxc exec salt-master -- groups salt
# Deve contenere "shadow"

# Se manca:
lxc exec salt-master -- usermod -a -G shadow salt
lxc exec salt-master -- systemctl restart salt-master salt-api
```

### Minion non si connette al master

```bash
# Verifica configurazione minion
lxc exec web-01 -- cat /etc/salt/minion | grep master

# Controlla log
lxc exec web-01 -- journalctl -u salt-minion -n 20

# Verifica chiavi sul master
lxc exec salt-master -- salt-key -L
```

### Il cert non arriva sul minion

```bash
# Testa il download direttamente dal minion
CERTMATE_IP=$(ip addr show lxdbr0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)
lxc exec web-01 -- curl -v \
  -H "Authorization: Bearer <token>" \
  "http://$CERTMATE_IP:8000/api/certificates/<domain>/download" \
  -o /tmp/test.zip
lxc exec web-01 -- unzip -l /tmp/test.zip
```

### Deploy hook non parte

```bash
# Verifica che i deploy hooks siano abilitati in settings.json
docker exec certmate-test python3 -c "
import json
s = json.load(open('/app/data/settings.json'))
dh = s.get('deploy_hooks', {})
print('enabled:', dh.get('enabled'))
print('hooks:', len(dh.get('global_hooks', [])))
"

# Testa il hook manualmente
docker exec -e CERTMATE_DOMAIN=example.com certmate-test /app/scripts/hook_salt_deploy.sh
```
