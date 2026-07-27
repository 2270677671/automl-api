#!/bin/sh
set -eu

usage() {
  echo "usage: $0 [ENV_FILE] [DATA_ROOT] [OIDC_PUBLIC_HOST] [OIDC_BIND_ADDRESS] [TENANT_ID]" >&2
  exit 2
}

[ "$#" -le 5 ] || usage

env_file=${1:-.env.production-single}
data_root=${2:-.automl-production}
public_host=${3:-}
bind_address=${4:-}
tenant_id=${5:-partner_a}

[ -f "$env_file" ] || {
  echo "existing production environment file not found: $env_file" >&2
  exit 1
}
command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required" >&2
  exit 1
}

if grep -q '^AUTOML_OIDC_' "$env_file"; then
  echo "refusing to modify an environment file that already contains OIDC settings" >&2
  exit 1
fi

read_setting() {
  setting_name=$1
  awk -F= -v name="$setting_name" '$1 == name {sub(/^[^=]*=/, ""); value=$0} END {print value}' \
    "$env_file"
}

public_host=${public_host:-$(read_setting AUTOML_PUBLIC_HOST)}
bind_address=${bind_address:-$(read_setting AUTOML_HTTPS_BIND_ADDRESS)}
bind_address=${bind_address:-$public_host}

for value in "$public_host" "$bind_address"; do
  case "$value" in
    *[!A-Za-z0-9._:-]* | "")
      echo "OIDC host and bind address must not contain whitespace, schemes, or paths" >&2
      exit 2
      ;;
  esac
done
case "$tenant_id" in
  *[!A-Za-z0-9._-]* | "")
    echo "TENANT_ID contains unsupported characters" >&2
    exit 2
    ;;
esac
case "$data_root" in
  *[!A-Za-z0-9._/+:=-]* | "")
    echo "DATA_ROOT contains unsupported characters" >&2
    exit 2
    ;;
esac

umask 077
mkdir -p "$data_root/identity-db"
chmod 0700 "$data_root" "$data_root/identity-db"

agent_client_secret=$(openssl rand -hex 48)
admin_password=$(openssl rand -hex 32)
db_password=$(openssl rand -hex 48)
temporary=$(mktemp "${env_file}.oidc.XXXXXX")
cleanup() {
  rm -f "$temporary"
}
trap cleanup EXIT HUP INT TERM

cp "$env_file" "$temporary"
printf '\n# OIDC/OAuth2 client-credentials identity service. Generated; do not commit.\n' \
  >>"$temporary"
cat >>"$temporary" <<EOF
AUTOML_OIDC_PUBLIC_HOST=$public_host
AUTOML_OIDC_HTTPS_BIND_ADDRESS=$bind_address
AUTOML_OIDC_HTTPS_PORT=9443
AUTOML_OIDC_REALM=automl
AUTOML_OIDC_AGENT_CLIENT_ID=automl-agent-platform
AUTOML_OIDC_AGENT_CLIENT_SECRET=$agent_client_secret
AUTOML_OIDC_AGENT_TENANT_ID=$tenant_id
AUTOML_OIDC_ADMIN_USERNAME=automl-admin
AUTOML_OIDC_ADMIN_PASSWORD=$admin_password
AUTOML_OIDC_DB_NAME=keycloak
AUTOML_OIDC_DB_USER=keycloak
AUTOML_OIDC_DB_PASSWORD=$db_password
AUTOML_OIDC_DB_HOST_DIR=$data_root/identity-db
AUTOML_OIDC_BACKUP_RETENTION_DAYS=14
AUTOML_KEYCLOAK_BASE_IMAGE=quay.nju.edu.cn/keycloak/keycloak:26.3.3@sha256:6a7217a100bd3e5de4063a27a538ef999a3c5a88c4b4ec0ffc0a642aee7b2597
AUTOML_KEYCLOAK_IMAGE=managed-automl-identity:26.3.3
AUTOML_POSTGRES_IMAGE=docker.m.daocloud.io/library/postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193
AUTOML_OIDC_CPU_LIMIT=2.0
AUTOML_OIDC_MEMORY_LIMIT=2g
AUTOML_OIDC_PIDS_LIMIT=512
AUTOML_OIDC_DB_CPU_LIMIT=1.0
AUTOML_OIDC_DB_MEMORY_LIMIT=1g
AUTOML_OIDC_DB_PIDS_LIMIT=256
EOF

chmod 0600 "$temporary"
mv "$temporary" "$env_file"
trap - EXIT HUP INT TERM

echo "OIDC settings added to $env_file; no credential values were printed"
echo "review the tenant, host, bind address, resource limits, and firewall before starting"
