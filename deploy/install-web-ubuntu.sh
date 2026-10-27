#!/usr/bin/env bash
# Native passive installation. Does not move source data or enable publication.
set -euo pipefail
umask 027
if [[ ${EUID} != 0 ]]; then echo 'Run this reviewed installer through sudo.' >&2; exit 1; fi
if [[ $# -lt 3 ]]; then echo 'Usage: install-web-ubuntu.sh RELEASE.zip EXPECTED_SHA256 SYSTEM_UUID [DOMAIN] [NODE_UUID]' >&2; exit 2; fi
archive=$(realpath -- "$1")
expected=$2
system_id=$3
domain=${4:-}
node_id=${5:-}
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || $(uname -m) != x86_64 ]]; then
  echo 'Supported installation profile: Ubuntu 24.04 LTS x86-64 only.' >&2; exit 1
fi
[[ "$expected" =~ ^[a-fA-F0-9]{64}$ ]] || { echo 'Invalid SHA-256' >&2; exit 2; }
[[ "$system_id" =~ ^[a-fA-F0-9-]{36}$ ]] || { echo 'Invalid system ID' >&2; exit 2; }
if [[ -n "$node_id" && ! "$node_id" =~ ^[a-fA-F0-9-]{36}$ ]]; then echo 'Invalid node ID' >&2; exit 2; fi
if [[ -n "$domain" && ! "$domain" =~ ^[A-Za-z0-9.-]+$ ]]; then echo 'Invalid DNS name' >&2; exit 2; fi
for managed_path in /var/lib/publisher /var/lib/publisher/data /var/lib/publisher/control /opt/publisher /opt/publisher/releases /etc/publisher; do
  [[ ! -L "$managed_path" ]] || { echo 'Managed installation paths cannot be symlinks.' >&2; exit 1; }
done
actual=$(sha256sum -- "$archive")
[[ "${actual%% *}" == "${expected,,}" ]] || { echo 'Release SHA-256 mismatch' >&2; exit 1; }
apt-get update
apt-get install --yes python3.12 python3.12-venv ca-certificates openssh-client
if id publisher >/dev/null 2>&1; then
  [[ $(getent passwd publisher | cut -d: -f6) == /var/lib/publisher ]] || { echo 'Existing publisher account has a different home; refusing to reuse it.' >&2; exit 1; }
else
  useradd --system --create-home --home-dir /var/lib/publisher --shell /bin/bash publisher
fi
install -d -m 0755 /opt/publisher/releases /opt/publisher/venvs /opt/publisher/archives
install -d -o publisher -g publisher -m 0700 /var/lib/publisher/control /var/lib/publisher/data
install -d -o root -g publisher -m 0750 /etc/publisher
stage=$(mktemp -d /opt/publisher/releases/.install-XXXXXXXX)
trap 'echo "Installer stopped; staging is preserved at $stage for diagnosis." >&2' ERR
release_id=$(python3.12 - "$archive" "$stage" <<'PY'
import hashlib,json,pathlib,stat,sys,zipfile
archive,root=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2])
with zipfile.ZipFile(archive) as bundle:
    manifest=json.loads(bundle.read('release-manifest.json'))
    assert manifest['format']=='publisher-release-v1'
    records=manifest['files']
    encoded=json.dumps(records,sort_keys=True,separators=(',',':')).encode()
    digest=hashlib.sha256(encoded).hexdigest()[:16]
    assert manifest['release_id'] in {'edizora-'+digest, 'publisher-'+digest}
    assert set(bundle.namelist())=={x['key'] for x in records}|{'release-manifest.json'}
    assert len(bundle.namelist())==len(set(bundle.namelist()))
    assert sum(x.file_size for x in bundle.infolist()) < 2*1024**3
    for record in records:
        key=record['key']; p=pathlib.PurePosixPath(key)
        assert not p.is_absolute() and '\\' not in key and ':' not in key and '..' not in p.parts
        info=bundle.getinfo(key); assert not stat.S_ISLNK(info.external_attr>>16)
        content=bundle.read(key)
        assert len(content)==record['size'] and hashlib.sha256(content).hexdigest()==record['sha256']
        target=root.joinpath(*p.parts); target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(content)
        target.chmod(0o755 if key.endswith('.sh') else 0o644)
    (root/'release-manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
print(manifest['release_id'])
PY
)
release="/opt/publisher/releases/$release_id"
if [[ -d "$release" ]]; then
  cmp -- "$stage/release-manifest.json" "$release/release-manifest.json" || { echo 'Existing release differs; refusing overwrite' >&2; exit 1; }
  echo "Release already installed: $release_id"
else
  mv -- "$stage" "$release"
fi
# Code stays root-owned; the service account must traverse every release directory.
chown -R root:publisher "$release"
find "$release" -type d -exec chmod 0750 {} +
venv="/opt/publisher/venvs/$release_id"
python3.12 -m venv "$venv"
"$venv/bin/python" -m pip install 'uv==0.11.30'
UV_PROJECT_ENVIRONMENT="$venv" "$venv/bin/uv" sync --directory "$release" --frozen --no-dev --no-editable --python "$venv/bin/python"
chown -R root:publisher "$venv"
find "$venv" -type d -exec chmod 0750 {} +
runuser -u publisher -- "$venv/bin/python" -c 'import app.web'
runuser -u publisher -- test -r "$release/frontend/dist/index.html"
if [[ ! -f /etc/publisher/host.env ]]; then
  cat > /etc/publisher/host.env <<EOF
PUBLISHER_CONTROL_DIR=/var/lib/publisher/control
PUBLISHER_DATA_ROOT=/var/lib/publisher/data
PUBLISHER_DATABASE_PATH=/var/lib/publisher/data/publisher.sqlite
PUBLISHER_FRONTEND_DIR=$release/frontend/dist
PUBLISHER_HOST=127.0.0.1
PUBLISHER_PORT=8000
EOF
  if [[ -n "$domain" ]]; then printf '%s\n' "PUBLISHER_ORIGIN=https://$domain" >> /etc/publisher/host.env; fi
  chmod 0640 /etc/publisher/host.env
  chown root:publisher /etc/publisher/host.env
fi
if [[ ! -f /var/lib/publisher/control/node.json ]]; then
  node_arguments=()
  if [[ -n "$node_id" ]]; then node_arguments=(--node-id "$node_id"); fi
  runuser -u publisher -- "$venv/bin/python" -m app.deployment.cli --control-dir /var/lib/publisher/control initialize \
    --system-id "$system_id" --data-root /var/lib/publisher/data --release-id "$release_id" --profile ubuntu-24.04-x86_64 "${node_arguments[@]}"
fi
release_archive="/opt/publisher/archives/$release_id.zip"
if [[ -e "$release_archive" ]]; then
  [[ $(sha256sum -- "$release_archive" | cut -d' ' -f1) == "${expected,,}" ]] || { echo 'Stored archive differs; refusing overwrite.' >&2; exit 1; }
else
  install -m 0644 "$archive" "$release_archive"
fi
runuser -u publisher -- "$venv/bin/python" -m app.deployment.cli --control-dir /var/lib/publisher/control register-release --archive "$release_archive"
# Preserve an existing active installation; switching a release is a separate
# stopped upgrade, never an installer side effect.
if [[ -L /opt/publisher/current ]]; then
  [[ $(readlink /opt/publisher/current) == "$release" ]] || { echo 'Another release is current. Perform the documented stopped upgrade.' >&2; exit 1; }
else
  ln -s -- "$release" /opt/publisher/current
fi
if [[ -e /etc/systemd/system/publisher-host.service ]] && ! grep -Eq '^Description=(Edizora|Publisher) managed host and runtime$' /etc/systemd/system/publisher-host.service; then
  echo 'An unrelated publisher-host.service already exists; refusing overwrite.' >&2; exit 1
fi
cat > /etc/systemd/system/publisher-host.service <<EOF
[Unit]
Description=Edizora managed host and runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=publisher
Group=publisher
WorkingDirectory=$release
EnvironmentFile=/etc/publisher/host.env
ExecStart=$venv/bin/python -m app.web --no-browser --enable-bot
Restart=on-failure
RestartSec=5
TimeoutStopSec=180
KillSignal=SIGTERM
KillMode=mixed
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/publisher

[Install]
WantedBy=multi-user.target
EOF
cat > /usr/local/bin/publisher-node-agent <<EOF
#!/bin/sh
export PUBLISHER_CONTROL_DIR=/var/lib/publisher/control
cd '$release'
exec '$venv/bin/python' -m app.deployment.agent "\$@"
EOF
chmod 0755 /usr/local/bin/publisher-node-agent
if [[ -n "$domain" ]]; then
  caddy_preexisting=false
  if command -v caddy >/dev/null 2>&1; then caddy_preexisting=true; fi
  apt-get install --yes caddy
  if [[ "$caddy_preexisting" == false ]]; then systemctl disable --now caddy; fi
  fragment=/etc/caddy/publisher.caddy
  temporary_fragment=$(mktemp)
  cat > "$temporary_fragment" <<EOF
$domain {
    reverse_proxy 127.0.0.1:8000 {
        header_up -X-User-ID
        header_up -X-Publisher-Context
        header_up -X-Publisher-Internal
    }
    encode zstd gzip
}
EOF
  if [[ -e "$fragment" ]]; then
    cmp -- "$temporary_fragment" "$fragment" || { echo 'Caddy app fragment differs; inspect before changing it.' >&2; exit 1; }
  else
    install -m 0644 "$temporary_fragment" "$fragment"
  fi
  echo "Caddy fragment prepared: $fragment"
  if [[ "$caddy_preexisting" == false ]]; then
    cat > /etc/systemd/system/publisher-proxy.service <<EOF
[Unit]
Description=Edizora dedicated HTTPS proxy
After=network-online.target publisher-host.service
Wants=network-online.target
[Service]
User=caddy
Group=caddy
Environment=HOME=/var/lib/caddy
ExecStart=/usr/bin/caddy run --config /etc/caddy/publisher.caddy --adapter caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/publisher.caddy --adapter caddyfile
Restart=on-failure
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    caddy validate --config "$fragment" --adapter caddyfile
    systemctl enable --now publisher-proxy
  else
    echo 'An existing Caddy installation was preserved. Include publisher.caddy in its configuration, validate and reload it; migration routing waits for the real HTTPS challenge.'
  fi
fi
systemctl daemon-reload
echo "Installed $release_id in standby. Data/control remain separate."
echo 'Review /etc/publisher/host.env; provision credentials locally. Start host: systemctl enable --now publisher-host'
echo 'No source data was moved and no publication authority was granted.'
