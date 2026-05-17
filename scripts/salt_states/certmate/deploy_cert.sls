# Salt State: certmate.deploy_cert
# Scarica il certificato (ZIP) da CertMate, lo estrae e riavvia il servizio
#
# Pillar richiesti:
#   certmate_domain  : dominio del certificato (es. demo.certmate.local)
#   certmate_url     : URL di CertMate raggiungibile dal minion
#   certmate_token   : API token di CertMate
#   service_restart  : servizio da riavviare (nginx, apache2, httpd)

{% set domain       = pillar.get('certmate_domain', '') %}
{% set certmate_url = pillar.get('certmate_url', 'http://localhost:8000') %}
{% set token        = pillar.get('certmate_token', '') %}
{% set service      = pillar.get('service_restart', 'nginx') %}
{% set cert_dir     = '/etc/nginx/ssl/' ~ domain %}
{% set zip_tmp      = '/tmp/certmate_' ~ domain ~ '.zip' %}

# 1. Crea cartella certificati
{{ cert_dir }}:
  file.directory:
    - makedirs: True
    - mode: 700

# 2. Scarica il ZIP dei certificati
download_cert_zip_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: >
        curl -sf
        -H "Authorization: Bearer {{ token }}"
        "{{ certmate_url }}/api/certificates/{{ domain }}/download"
        -o {{ zip_tmp }}
    - require:
      - file: {{ cert_dir }}

# 3. Estrai il ZIP nella cartella destinazione
extract_cert_zip_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: unzip -o {{ zip_tmp }} -d {{ cert_dir }}
    - require:
      - cmd: download_cert_zip_{{ domain | replace('.', '_') }}

# 4. Imposta permessi sicuri sulla chiave privata
{{ cert_dir }}/privkey.pem:
  file.managed:
    - mode: 600
    - replace: False
    - require:
      - cmd: extract_cert_zip_{{ domain | replace('.', '_') }}

# 5. Pulizia ZIP temporaneo
cleanup_zip_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: rm -f {{ zip_tmp }}
    - require:
      - cmd: extract_cert_zip_{{ domain | replace('.', '_') }}

# 6. Riavvia il servizio
{{ service }}_reload_{{ domain | replace('.', '_') }}:
  service.running:
    - name: {{ service }}
    - reload: True
    - watch:
      - cmd: extract_cert_zip_{{ domain | replace('.', '_') }}
