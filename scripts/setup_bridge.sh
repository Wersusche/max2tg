#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(dirname "$SCRIPT_DIR")
SECRETS_DIR="$APP_DIR/secrets"
KEY_PATH="$SECRETS_DIR/foreign.key"
RELAY_ENV_PATH="$SECRETS_DIR/relay.env"
BRIDGE_ENV_PATH="$APP_DIR/.env"

foreign_admin=""
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
    foreign_port="${foreign_port:-$(env_get "$BRIDGE_ENV_PATH" FOREIGN_SSH_PORT)}"
    foreign_user="${foreign_user:-$(env_get "$BRIDGE_ENV_PATH" FOREIGN_SSH_USER)}"
    foreign_app_dir="${foreign_app_dir:-$(env_get "$BRIDGE_ENV_PATH" FOREIGN_APP_DIR)}"
    max_token="${max_token:-$(env_get "$BRIDGE_ENV_PATH" MAX_TOKEN)}"
    max_device_id="${max_device_id:-$(env_get "$BRIDGE_ENV_PATH" MAX_DEVICE_ID)}"
    max_chat_ids="${max_chat_ids:-$(env_get "$BRIDGE_ENV_PATH" MAX_CHAT_IDS)}"
    tg_bot_token="${tg_bot_token:-$(env_get "$RELAY_ENV_PATH" TG_BOT_TOKEN)}"
    tg_chat_id="${tg_chat_id:-$(env_get "$RELAY_ENV_PATH" TG_CHAT_ID)}"
    reply_enabled="${reply_enabled:-$(env_get "$BRIDGE_ENV_PATH" REPLY_ENABLED)}"
    debug="${debug:-$(env_get "$BRIDGE_ENV_PATH" DEBUG)}"
    relay_host_port="${relay_host_port:-$(env_get "$RELAY_ENV_PATH" RELAY_HOST_PORT)}"
}

prompt_missing_values() {
    [ -n "$foreign_admin" ] || prompt_value foreign_admin "Foreign admin SSH target (for example root@relay.example.com)"
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

ensure_key_pair() {
    need_command ssh-keygen
    mkdir -p "$SECRETS_DIR"
    chmod 700 "$SECRETS_DIR" || true

    if [ ! -f "$KEY_PATH" ]; then
        log "Generating SSH key at $KEY_PATH"
        ssh-keygen -t ed25519 -N "" -f "$KEY_PATH" -C max2tg-relay >/dev/null
    fi

    chmod 600 "$KEY_PATH" || true
    if [ ! -f "$KEY_PATH.pub" ]; then
        ssh-keygen -y -f "$KEY_PATH" > "$KEY_PATH.pub"
    fi
}

write_env_files() {
    local shared_secret
    shared_secret="$(env_get "$BRIDGE_ENV_PATH" RELAY_SHARED_SECRET)"
    [ -n "$shared_secret" ] || shared_secret="$(random_secret)"

    log "Writing $RELAY_ENV_PATH"
    cat > "$RELAY_ENV_PATH" <<EOF
APP_ROLE=tg-relay
RELAY_SHARED_SECRET=$shared_secret
TG_BOT_TOKEN=$tg_bot_token
TG_CHAT_ID=$tg_chat_id
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
REPLY_ENABLED=$reply_enabled
DEBUG=$debug

RELAY_TUNNEL_LOCAL_PORT=18080
FOREIGN_SSH_HOST=${foreign_admin#*@}
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

    chmod 600 "$BRIDGE_ENV_PATH" "$RELAY_ENV_PATH" || true
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

    local foreign_host
    foreign_host="${foreign_admin#*@}"
    log "Verifying relay SSH access to $foreign_user@$foreign_host"
    ssh -p "$foreign_port" -i "$KEY_PATH" \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new \
        "$foreign_user@$foreign_host" "true"
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
    write_env_files
    prepare_foreign_host
    verify_relay_ssh

    if [ "$start_compose" = "true" ]; then
        start_bridge
    else
        log "Skipped Docker Compose start because --no-start was passed."
    fi

    log "Done."
}

main "$@"
