{% set domain      = pillar.get('certmate_domain', '') %}
{% set cert_dir    = pillar.get('deploy_path', '/etc/nginx/ssl/' ~ domain) %}
{% set service     = pillar.get('service_restart', 'nginx') %}
{% set restart_cmd = pillar.get('restart_cmd', '') %}

remove_cert_dir_{{ domain | replace('.', '_') }}:
  file.absent:
    - name: {{ cert_dir }}

{% if restart_cmd %}
reload_service_after_remove_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: {{ restart_cmd }}
    - require:
      - file: remove_cert_dir_{{ domain | replace('.', '_') }}
{% else %}
{{ service }}_reload_after_remove_{{ domain | replace('.', '_') }}:
  service.running:
    - name: {{ service }}
    - reload: True
    - require:
      - file: remove_cert_dir_{{ domain | replace('.', '_') }}
{% endif %}
