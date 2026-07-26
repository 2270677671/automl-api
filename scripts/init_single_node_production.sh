#!/bin/sh
set -eu

usage() {
  echo "usage: $0 PUBLIC_HOST [HTTPS_BIND_ADDRESS] [ENV_FILE] [DATA_ROOT]" >&2
  exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 4 ] || usage

public_host=$1
bind_address=${2:-0.0.0.0}
env_file=${3:-.env.production-single}
data_root=${4:-.automl-production}

case "$public_host" in
  *[!A-Za-z0-9._:-]* | "")
    echo "PUBLIC_HOST must be a DNS name or an IPv4/IPv6 address without a scheme or path" >&2
    exit 2
    ;;
esac
case "$bind_address" in
  *[!A-Za-z0-9._:-]* | "")
    echo "HTTPS_BIND_ADDRESS must be an address without a port" >&2
    exit 2
    ;;
esac

if [ -e "$env_file" ]; then
  echo "refusing to overwrite existing $env_file" >&2
  exit 1
fi
if [ ! -f .env.production-single.example ]; then
  echo "run this command from the repository root" >&2
  exit 1
fi
command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required" >&2
  exit 1
}

umask 077
mkdir -p "$data_root/state" "$data_root/backups" "$data_root/caddy-data" "$data_root/caddy-config"
chmod 0700 "$data_root" "$data_root/state" "$data_root/backups" \
  "$data_root/caddy-data" "$data_root/caddy-config"

jwt_secret=$(openssl rand -hex 48)
cursor_secret=$(openssl rand -hex 48)
ticket_secret=$(openssl rand -hex 48)

sed \
  -e "s|^AUTOML_PUBLIC_HOST=.*|AUTOML_PUBLIC_HOST=$public_host|" \
  -e "s|^AUTOML_HTTPS_BIND_ADDRESS=.*|AUTOML_HTTPS_BIND_ADDRESS=$bind_address|" \
  -e "s|^AUTOML_JWT_ISSUER=.*|AUTOML_JWT_ISSUER=https://$public_host/identity|" \
  -e "s|^AUTOML_JWT_SECRET=.*|AUTOML_JWT_SECRET=$jwt_secret|" \
  -e "s|^AUTOML_CURSOR_SECRET=.*|AUTOML_CURSOR_SECRET=$cursor_secret|" \
  -e "s|^AUTOML_TICKET_SECRET=.*|AUTOML_TICKET_SECRET=$ticket_secret|" \
  -e "s|^AUTOML_STATE_HOST_DIR=.*|AUTOML_STATE_HOST_DIR=$data_root/state|" \
  -e "s|^AUTOML_BACKUP_HOST_DIR=.*|AUTOML_BACKUP_HOST_DIR=$data_root/backups|" \
  -e "s|^AUTOML_CADDY_DATA_HOST_DIR=.*|AUTOML_CADDY_DATA_HOST_DIR=$data_root/caddy-data|" \
  -e "s|^AUTOML_CADDY_CONFIG_HOST_DIR=.*|AUTOML_CADDY_CONFIG_HOST_DIR=$data_root/caddy-config|" \
  .env.production-single.example >"$env_file"
chmod 0600 "$env_file"

echo "created $env_file and protected runtime directories"
echo "review resource limits, TabPFN settings, and the bind address before starting"
