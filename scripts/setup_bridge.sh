#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(dirname "$SCRIPT_DIR")
SECRETS_DIR="$APP_DIR/secrets"
KEY_PATH="$SECRETS_DIR/foreign.key"
RELAY_ENV_PATH="$SECRETS_DIR/relay.env"
ACCOUNTS_CONFIG_PATH="$SECRETS_DIR/accounts.yaml"
BRIDGE_ENV_PATH="$APP_DIR/.env"

foreign_admin=""
foreign_host=""
foreign_port=""
foreign_user=""
foreign_app_dir=""
max_token=""
max_device_id=""
tg_bot_token=""
tg_chat_id=""
reply_enabled=""
debug=""
max_chat_ids=""
relay_host_port=""
start_compose="true"

usage() {
    cat <<'USAGE'
Usage:
  bash scripts/setup_bridge.sh --foreign-admin root@relay.example.com \
    --max-token TOKEN --max-device-id DEVICE_ID \
    --tg-bot-token BOT_TOKEN --tg-chat-id -1001234567890

Options:
  --foreign-admin USER@HOST   SSH admin account for first bootstrap.
  --foreign-host HOST         Foreign server host/IP. Usually read from existing .env.
  --foreign-port PORT         SSH port for both admin and relay users (default: 22).
  --foreign-user USER         Relay deploy user to create/use (default: relay).
  --foreign-app-dir PATH      App directory on the foreign host (default: /home/relay/max2tg).
  --max-token TOKEN           Max __oneme_auth value.
  --max-device-id ID          Max __oneme_device_id value.
  --max-chat-ids IDS          Optional comma-separated Max chat IDs.
  --tg-bot-token TOKEN        Telegram bot token.
  --tg-chat-id ID             Telegram forum supergroup ID.
  --reply-enabled true|false  Enable Telegram -> Max replies (default: true).
  --debug true|false          Enable verbose logs (default: false).
  --relay-host-port PORT      Optional foreign localhost port if 8080 is busy.
  --no-start                  Write config and prepare foreign host, but do not start compose.
  -h, --help                  Show this help.
USAGE
}

log() {
    printf '[setup] %s\n' "$*" >&2
}

fail() {
    printf '[setup] ERROR: %s\n' "$*" >&2
    exit 1
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command is missing: $1"
}

env_get() {
    local file="$1"
    local key="$2"
    [ -f "$file" ] || return 0
    awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$file"
}

decode_env_scalar() {
    local value="$1"

    case "$value" in
        \"*\")
            value="${value#\"}"
            value="${value%\"}"
            ;;
        \'*\')
            value="${value#\'}"
            value="${value%\'}"
            ;;
    esac

    printf '%b' "$value"
}

relay_env_file_from_bridge_env() {
    local configured_path=""
    local mapped_path=""

    configured_path="$(env_get "$BRIDGE_ENV_PATH" FOREIGN_RELAY_ENV_FILE)"
    [ -n "$configured_path" ] || return 0

    case "$configured_path" in
        /run/max2tg-secrets/*)
            mapped_path="$SECRETS_DIR/${configured_path#/run/max2tg-secrets/}"
            ;;
        *)
            mapped_path="$configured_path"
            ;;
    esac

    if [ -f "$mapped_path" ]; then
        printf '%s\n' "$mapped_path"
    fi
}

legacy_relay_env_get() {
    local key="$1"
    local relay_env_b64=""

    relay_env_b64="$(env_get "$BRIDGE_ENV_PATH" FOREIGN_RELAY_ENV_B64)"
    [ -n "$relay_env_b64" ] || return 0

    printf '%s' "$relay_env_b64" \
        | base64 -d 2>/dev/null \
        | awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }'
}

prompt_value() {
    local var_name="$1"
    local label="$2"
    local default_value="${3:-}"
    local value=""

    if [ -n "$default_value" ]; then
        read -r -p "$label [$default_value]: " value
        value="${value:-$default_value}"
    else
        while [ -z "$value" ]; do
            read -r -p "$label: " value
        done
    fi

    printf -v "$var_name" '%s' "$value"
}

prompt_secret() {
    local var_name="$1"
    local label="$2"
    local default_value="${3:-}"
    local value=""

    if [ -n "$default_value" ]; then
        read -r -s -p "$label [keep existing]: " value
        printf '\n' >&2
        value="${value:-$default_value}"
    else
        while [ -z "$value" ]; do
            read -r -s -p "$label: " value
            printf '\n' >&2
        done
    fi

    printf -v "$var_name" '%s' "$value"
}

random_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return 0
    fi
    if [ -r /dev/urandom ]; then
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
        return 0
    fi
    fail "Cannot generate RELAY_SHARED_SECRET: install openssl or provide /dev/urandom."
}

validate_safe_remote_inputs() {
    case "$foreign_host" in
        ''|*\'*|*$'\n'*)
            fail "foreign host must be a non-empty SSH host without quotes or newlines."
            ;;
    esac
    case "$foreign_admin" in
        *\'*|*$'\n'*|*" "*)
            fail "foreign admin target must not contain quotes, spaces, or newlines."
            ;;
    esac
    if [ -n "$foreign_admin" ]; then
        case "$foreign_admin" in
            *@*) ;;
            *)
                fail "foreign admin target must look like user@host. Do not paste a password here; ssh will ask for it later."
                ;;
        esac
    fi
    case "$foreign_host" in
        *" "*)
            fail "foreign host must not contain spaces."
            ;;
    esac
    case "$foreign_user" in
        ''|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-]*)
            fail "foreign user must contain only letters, digits, underscore, or dash."
            ;;
    esac
    case "$foreign_app_dir" in
        ''|*\'*|*$'\n'*)
            fail "foreign app dir must be a non-empty path without quotes or newlines."
            ;;
    esac
    case "$foreign_port" in
        ''|*[!0123456789]*)
            fail "foreign port must be an integer."
            ;;
    esac
    case "$reply_enabled" in true|false|1|0|yes|no) ;; *) fail "--reply-enabled must be true or false." ;; esac
    case "$debug" in true|false|1|0|yes|no) ;; *) fail "--debug must be true or false." ;; esac
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --foreign-admin) foreign_admin="${2:-}"; shift 2 ;;
            --foreign-host) foreign_host="${2:-}"; shift 2 ;;
            --foreign-port) foreign_port="${2:-}"; shift 2 ;;
            --foreign-user) foreign_user="${2:-}"; shift 2 ;;
            --foreign-app-dir) foreign_app_dir="${2:-}"; shift 2 ;;
            --max-token) max_token="${2:-}"; shift 2 ;;
            --max-device-id) max_device_id="${2:-}"; shift 2 ;;
            --max-chat-ids) max_chat_ids="${2:-}"; shift 2 ;;
            --tg-bot-token) tg_bot_token="${2:-}"; shift 2 ;;
            --tg-chat-id) tg_chat_id="${2:-}"; shift 2 ;;
            --reply-enabled) reply_enabled="${2:-}"; shift 2 ;;
            --debug) debug="${2:-}"; shift 2 ;;
            --relay-host-port) relay_host_port="${2:-}"; shift 2 ;;
            --no-start) start_compose="false"; shift ;;
            -h|--help) usage; exit 0 ;;
            *) fail "Unknown option: $1" ;;
        esac
    done
}

load_existing_defaults() {
    local relay_env_defaults_path=""
    local configured_foreign_host=""

    relay_env_defaults_path="$(relay_env_file_from_bridge_env)"
    if [ -z "$relay_env_defaults_path" ] && [ -f "$RELAY_ENV_PATH" ]; then
        relay_env_defaults_path="$RELAY_ENV_PATH"
    fi

    configured_foreign_host="$(env_get "$BRIDGE_ENV_PATH" FOREIGN_SSH_HOST)"
    foreign_host="${foreign_host:-$configured_foreign_host}"
    if [ -z "$foreign_host" ] && [ -n "$foreign_admin" ]; then
        foreign_host="${foreign_admin#*@}"
    fi

    foreign_port="${foreign_port:-$(env_get "$BRIDGE_ENV_PATH" FOREIGN_SSH_PORT)}"
    foreign_user="${foreign_user:-$(env_get "$BRIDGE_ENV_PATH" FOREIGN_SSH_USER)}"
    foreign_app_dir="${foreign_app_dir:-$(env_get "$BRIDGE_ENV_PATH" FOREIGN_APP_DIR)}"
    max_token="${max_token:-$(env_get "$BRIDGE_ENV_PATH" MAX_TOKEN)}"
    max_device_id="${max_device_id:-$(env_get "$BRIDGE_ENV_PATH" MAX_DEVICE_ID)}"
    max_chat_ids="${max_chat_ids:-$(env_get "$BRIDGE_ENV_PATH" MAX_CHAT_IDS)}"
    if [ -n "$relay_env_defaults_path" ]; then
        tg_bot_token="${tg_bot_token:-$(env_get "$relay_env_defaults_path" TG_BOT_TOKEN)}"
        tg_chat_id="${tg_chat_id:-$(env_get "$relay_env_defaults_path" TG_CHAT_ID)}"
        relay_host_port="${relay_host_port:-$(env_get "$relay_env_defaults_path" RELAY_HOST_PORT)}"
        reply_enabled="${reply_enabled:-$(env_get "$relay_env_defaults_path" REPLY_ENABLED)}"
        debug="${debug:-$(env_get "$relay_env_defaults_path" DEBUG)}"
    fi
    tg_bot_token="${tg_bot_token:-$(legacy_relay_env_get TG_BOT_TOKEN)}"
    tg_chat_id="${tg_chat_id:-$(legacy_relay_env_get TG_CHAT_ID)}"
    relay_host_port="${relay_host_port:-$(legacy_relay_env_get RELAY_HOST_PORT)}"
    reply_enabled="${reply_enabled:-$(legacy_relay_env_get REPLY_ENABLED)}"
    debug="${debug:-$(legacy_relay_env_get DEBUG)}"
    reply_enabled="${reply_enabled:-$(env_get "$BRIDGE_ENV_PATH" REPLY_ENABLED)}"
    debug="${debug:-$(env_get "$BRIDGE_ENV_PATH" DEBUG)}"
}

prompt_missing_values() {
    [ -n "$foreign_host" ] || prompt_value foreign_host "Foreign server host/IP"
    [ -n "$foreign_port" ] || prompt_value foreign_port "Foreign SSH port" "22"
    [ -n "$foreign_user" ] || prompt_value foreign_user "Foreign relay user" "relay"
    [ -n "$foreign_app_dir" ] || prompt_value foreign_app_dir "Foreign app directory" "/home/relay/max2tg"
    [ -n "$max_token" ] || prompt_secret max_token "Max __oneme_auth"
    [ -n "$max_device_id" ] || prompt_secret max_device_id "Max __oneme_device_id"
    [ -n "$tg_bot_token" ] || prompt_secret tg_bot_token "Telegram bot token"
    [ -n "$tg_chat_id" ] || prompt_value tg_chat_id "Telegram forum chat id"
    [ -n "$reply_enabled" ] || prompt_value reply_enabled "Enable Telegram -> Max replies" "true"
    [ -n "$debug" ] || prompt_value debug "Enable debug logs" "false"
}

prompt_foreign_admin() {
    log "Relay SSH access is not ready yet; foreign admin access is needed for one-time bootstrap."
    log "Press Enter to use root@$foreign_host. Do not paste the root password here; ssh will ask for it separately."
    while true; do
        [ -n "$foreign_admin" ] || prompt_value foreign_admin "Foreign admin SSH target" "root@$foreign_host"
        case "$foreign_admin" in
            *@*) return 0 ;;
            *)
                log "That does not look like user@host. If you pasted a password, press Enter next time and wait for ssh's password prompt."
                foreign_admin=""
                ;;
        esac
    done
}

ensure_key_pair() {
    need_command ssh-keygen
    mkdir -p "$SECRETS_DIR"
    chmod 700 "$SECRETS_DIR" || true

    if [ ! -f "$KEY_PATH" ]; then
        local legacy_key=""
        legacy_key="$(env_get "$BRIDGE_ENV_PATH" FOREIGN_SSH_PRIVATE_KEY)"
        if [ -n "$legacy_key" ]; then
            log "Migrating FOREIGN_SSH_PRIVATE_KEY from .env to $KEY_PATH"
            decode_env_scalar "$legacy_key" > "$KEY_PATH"
            printf '\n' >> "$KEY_PATH"
        else
            log "Generating SSH key at $KEY_PATH"
            ssh-keygen -t ed25519 -N "" -f "$KEY_PATH" -C max2tg-relay >/dev/null
        fi
    fi

    chmod 600 "$KEY_PATH" || true
    ssh-keygen -y -f "$KEY_PATH" > "$KEY_PATH.pub" || fail "Could not read SSH private key at $KEY_PATH"
}

try_legacy_private_key_for_relay_ssh() {
    local legacy_key=""
    local temp_key=""

    legacy_key="$(env_get "$BRIDGE_ENV_PATH" FOREIGN_SSH_PRIVATE_KEY)"
    [ -n "$legacy_key" ] || return 1

    log "Existing relay SSH check failed; retrying with FOREIGN_SSH_PRIVATE_KEY from .env"
    temp_key="$SECRETS_DIR/foreign.key.legacy.$$"
    decode_env_scalar "$legacy_key" > "$temp_key"
    printf '\n' >> "$temp_key"
    chmod 600 "$temp_key" || true

    if ! ssh-keygen -y -f "$temp_key" > "$temp_key.pub" 2>/dev/null; then
        rm -f "$temp_key" "$temp_key.pub"
        return 1
    fi

    if ssh_works_with_key "$temp_key"; then
        mv "$temp_key" "$KEY_PATH"
        mv "$temp_key.pub" "$KEY_PATH.pub"
        chmod 600 "$KEY_PATH" || true
        log "Migrated working FOREIGN_SSH_PRIVATE_KEY from .env to $KEY_PATH"
        return 0
    fi

    rm -f "$temp_key" "$temp_key.pub"
    return 1
}

write_env_files() {
    local shared_secret
    shared_secret="$(env_get "$BRIDGE_ENV_PATH" RELAY_SHARED_SECRET)"
    [ -n "$shared_secret" ] || shared_secret="$(random_secret)"

    if [ ! -f "$ACCOUNTS_CONFIG_PATH" ]; then
        log "Writing initial $ACCOUNTS_CONFIG_PATH"
        cat > "$ACCOUNTS_CONFIG_PATH" <<EOF
version: 1
profiles:
  - id: default
    label: Default
    enabled: true
    max:
      token: "$max_token"
      device_id: "$max_device_id"
      chat_ids: "$max_chat_ids"
    telegram:
      bot_token: "$tg_bot_token"
      chat_id: "$tg_chat_id"
EOF
    else
        log "Keeping existing $ACCOUNTS_CONFIG_PATH"
    fi

    log "Writing $RELAY_ENV_PATH"
    cat > "$RELAY_ENV_PATH" <<EOF
APP_ROLE=tg-relay
RELAY_SHARED_SECRET=$shared_secret
TG_BOT_TOKEN=$tg_bot_token
TG_CHAT_ID=$tg_chat_id
ACCOUNTS_CONFIG_FILE=/run/max2tg-secrets/accounts.yaml
RELAY_BIND_HOST=0.0.0.0
RELAY_BIND_PORT=8080
TOPIC_DB_PATH=data/topics.sqlite3
COMMAND_DB_PATH=data/commands.sqlite3
MESSAGE_DB_PATH=data/messages.sqlite3
REPLY_ENABLED=$reply_enabled
DEBUG=$debug
EOF
    if [ -n "$relay_host_port" ]; then
        printf 'RELAY_HOST_PORT=%s\n' "$relay_host_port" >> "$RELAY_ENV_PATH"
    fi

    log "Writing $BRIDGE_ENV_PATH"
    cat > "$BRIDGE_ENV_PATH" <<EOF
APP_ROLE=max-bridge
RELAY_SHARED_SECRET=$shared_secret

MAX_TOKEN=$max_token
MAX_DEVICE_ID=$max_device_id
ACCOUNTS_CONFIG_FILE=/run/max2tg-secrets/accounts.yaml
REPLY_ENABLED=$reply_enabled
DEBUG=$debug

RELAY_TUNNEL_LOCAL_PORT=18080
FOREIGN_SSH_HOST=$foreign_host
FOREIGN_SSH_PORT=$foreign_port
FOREIGN_SSH_USER=$foreign_user
FOREIGN_SSH_PRIVATE_KEY_FILE=/run/max2tg-secrets/foreign.key
FOREIGN_APP_DIR=$foreign_app_dir

REMOTE_DEPLOY_ENABLED=true
FOREIGN_RELAY_ENV_FILE=/run/max2tg-secrets/relay.env
EOF
    if [ -n "$max_chat_ids" ]; then
        printf 'MAX_CHAT_IDS=%s\n' "$max_chat_ids" >> "$BRIDGE_ENV_PATH"
    fi

    chmod 600 "$BRIDGE_ENV_PATH" "$RELAY_ENV_PATH" "$ACCOUNTS_CONFIG_PATH" || true
}

prepare_foreign_host() {
    need_command ssh
    need_command base64

    local pub_key_b64
    pub_key_b64="$(base64 < "$KEY_PATH.pub" | tr -d '\n')"

    log "Preparing foreign host through $foreign_admin"
    ssh -p "$foreign_port" -o StrictHostKeyChecking=accept-new "$foreign_admin" \
        "RELAY_USER='$foreign_user' APP_DIR='$foreign_app_dir' PUB_KEY_B64='$pub_key_b64' sh -s" <<'REMOTE_SETUP'
set -eu

fail() {
    echo "$*" >&2
    exit 1
}

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
        return 0
    fi
    if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
        sudo "$@"
        return 0
    fi
    fail "Admin SSH user must be root or have passwordless sudo."
}

if ! id "$RELAY_USER" >/dev/null 2>&1; then
    if command -v adduser >/dev/null 2>&1; then
        run_as_root adduser --disabled-password --gecos "" "$RELAY_USER"
    else
        run_as_root useradd -m -s /bin/sh "$RELAY_USER"
    fi
fi

if command -v getent >/dev/null 2>&1; then
    home_dir=$(getent passwd "$RELAY_USER" | cut -d: -f6 || true)
else
    home_dir=""
fi
[ -n "$home_dir" ] || home_dir="/home/$RELAY_USER"

run_as_root install -d -m 700 -o "$RELAY_USER" -g "$RELAY_USER" "$home_dir/.ssh"
run_as_root install -d -m 755 -o "$RELAY_USER" -g "$RELAY_USER" "$APP_DIR"
run_as_root install -d -m 755 -o "$RELAY_USER" -g "$RELAY_USER" "$APP_DIR/data"
run_as_root install -d -m 755 -o "$RELAY_USER" -g "$RELAY_USER" "$APP_DIR/logs"

tmp_key="/tmp/max2tg-authorized-key.$$"
printf '%s' "$PUB_KEY_B64" | base64 -d > "$tmp_key"
run_as_root install -m 600 -o "$RELAY_USER" -g "$RELAY_USER" "$tmp_key" "$home_dir/.ssh/authorized_keys"
rm -f "$tmp_key"

tmp_sudoers="/tmp/max2tg-sudoers.$$"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$RELAY_USER" > "$tmp_sudoers"
run_as_root install -m 440 "$tmp_sudoers" /etc/sudoers.d/90-relay-max2tg
rm -f "$tmp_sudoers"

if command -v visudo >/dev/null 2>&1; then
    run_as_root visudo -cf /etc/sudoers.d/90-relay-max2tg >/dev/null
fi
REMOTE_SETUP
}

verify_relay_ssh() {
    need_command ssh

    log "Verifying relay SSH access to $foreign_user@$foreign_host"
    ssh -p "$foreign_port" -i "$KEY_PATH" \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new \
        "$foreign_user@$foreign_host" "true"
}

relay_ssh_works() {
    need_command ssh

    ssh_works_with_key "$KEY_PATH"
}

ssh_works_with_key() {
    local ssh_key_path="$1"

    ssh -p "$foreign_port" -i "$ssh_key_path" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        "$foreign_user@$foreign_host" "true" >/dev/null 2>&1
}

start_bridge() {
    log "Starting bridge with Docker Compose"
    sh "$APP_DIR/scripts/bootstrap_remote.sh"
}

main() {
    parse_args "$@"
    load_existing_defaults
    prompt_missing_values
    validate_safe_remote_inputs
    ensure_key_pair

    if relay_ssh_works; then
        log "Existing relay SSH access works; skipping foreign admin bootstrap."
    elif try_legacy_private_key_for_relay_ssh; then
        log "Existing relay SSH access works after legacy key migration; skipping foreign admin bootstrap."
    else
        prompt_foreign_admin
        validate_safe_remote_inputs
        prepare_foreign_host
        verify_relay_ssh
    fi

    write_env_files

    if [ "$start_compose" = "true" ]; then
        start_bridge
    else
        log "Skipped Docker Compose start because --no-start was passed."
    fi

    log "Done."
}

main "$@"
