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
        curl -sf
        -H "Authorization: Bearer {{ token }}"
        "{{ certmate_url }}/api/certificates/{{ domain }}/download"
        -o {{ zip_tmp }}
    - require:
      - file: {{ cert_dir }}

extract_cert_zip_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: python3 -c "import zipfile; zipfile.ZipFile('{{ zip_tmp }}').extractall('{{ cert_dir }}')"
    - require:
      - cmd: download_cert_zip_{{ domain | replace('.', '_') }}

{{ cert_dir }}/privkey.pem:
  file.managed:
    - mode: 600
    - replace: False
    - require:
      - cmd: extract_cert_zip_{{ domain | replace('.', '_') }}

cleanup_zip_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: rm -f {{ zip_tmp }}
    - require:
      - cmd: extract_cert_zip_{{ domain | replace('.', '_') }}

{% if restart_cmd %}
reload_service_{{ domain | replace('.', '_') }}:
  cmd.run:
    - name: {{ restart_cmd }}
    - require:
      - cmd: extract_cert_zip_{{ domain | replace('.', '_') }}
{% else %}
{{ service }}_reload_{{ domain | replace('.', '_') }}:
  service.running:
    - name: {{ service }}
    - reload: True
    - watch:
      - cmd: extract_cert_zip_{{ domain | replace('.', '_') }}
{% endif %}
