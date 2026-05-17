# Salt State: certmate.remove_cert
# Rimuove la directory del certificato dal minion e ricarica il servizio.
# Chiamato da CertMate quando si cancella un certificato dalla dashboard.
#
# Pillar richiesti:
#   certmate_domain  : dominio del certificato
#   service_restart  : servizio da ricaricare dopo la rimozione
#   deploy_path      : (opzionale) path da rimuovere
#                      default: /etc/nginx/ssl/<domain>

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
