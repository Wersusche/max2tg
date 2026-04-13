#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_DIR=$(dirname "$SCRIPT_DIR")
COMPOSE_FILE_PATH="$APP_DIR/docker-compose.yml"
cd "$APP_DIR"

require_project_file() {
    file_path="$1"
    if [ -f "$file_path" ]; then
        return 0
    fi
    echo "Required deploy file is missing: $file_path" >&2
    exit 1
}

have_passwordless_sudo() {
    command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1
}

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
        return 0
    fi
    if have_passwordless_sudo; then
        sudo "$@"
        return 0
    fi
    echo "Automatic bootstrap requires root or passwordless sudo on the remote host." >&2
    exit 1
}

fetch_url() {
    url="$1"
    output_path="${2:-}"

    if command -v curl >/dev/null 2>&1; then
        if [ -n "$output_path" ]; then
            curl -fsSL "$url" -o "$output_path"
        else
            curl -fsSL "$url"
        fi
        return 0
    fi

    if command -v wget >/dev/null 2>&1; then
        if [ -n "$output_path" ]; then
            wget -qO "$output_path" "$url"
        else
            wget -qO- "$url"
        fi
        return 0
    fi

    echo "Neither curl nor wget is available on the remote host." >&2
    exit 1
}

ensure_fetcher() {
    if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
        return 0
    fi

    if command -v apt-get >/dev/null 2>&1; then
        run_as_root apt-get update
        run_as_root apt-get install -y curl
        return 0
    fi

    if command -v dnf >/dev/null 2>&1; then
        run_as_root dnf install -y curl
        return 0
    fi

    if command -v yum >/dev/null 2>&1; then
        run_as_root yum install -y curl
        return 0
    fi

    if command -v apk >/dev/null 2>&1; then
        run_as_root apk add --no-cache curl
        return 0
    fi

    echo "Unable to install curl or wget automatically on the remote host." >&2
    exit 1
}

docker_accessible() {
    docker info >/dev/null 2>&1
}

docker_accessible_via_sudo() {
    have_passwordless_sudo && sudo docker info >/dev/null 2>&1
}

ensure_docker() {
    if command -v docker >/dev/null 2>&1; then
        return 0
    fi

    ensure_fetcher
    installer_path=$(mktemp)
    fetch_url "https://get.docker.com" "$installer_path"
    run_as_root sh "$installer_path"
    rm -f "$installer_path"
}

ensure_docker_service() {
    if command -v systemctl >/dev/null 2>&1; then
        run_as_root systemctl enable --now docker >/dev/null 2>&1 || run_as_root systemctl start docker >/dev/null 2>&1 || true
        return 0
    fi

    if command -v service >/dev/null 2>&1; then
        run_as_root service docker start >/dev/null 2>&1 || true
    fi
}

has_compose_plugin() {
    docker compose version >/dev/null 2>&1 || (have_passwordless_sudo && sudo docker compose version >/dev/null 2>&1)
}

has_compose_binary() {
    (command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1) || (have_passwordless_sudo && sudo docker-compose version >/dev/null 2>&1)
}

install_compose_package() {
    if command -v apt-get >/dev/null 2>&1; then
        run_as_root apt-get update
        run_as_root apt-get install -y docker-compose-plugin >/dev/null 2>&1 || run_as_root apt-get install -y docker-compose >/dev/null 2>&1
        return $?
    fi

    if command -v dnf >/dev/null 2>&1; then
        run_as_root dnf install -y docker-compose-plugin >/dev/null 2>&1 || run_as_root dnf install -y docker-compose >/dev/null 2>&1
        return $?
    fi

    if command -v yum >/dev/null 2>&1; then
        run_as_root yum install -y docker-compose-plugin >/dev/null 2>&1 || run_as_root yum install -y docker-compose >/dev/null 2>&1
        return $?
    fi

    if command -v apk >/dev/null 2>&1; then
        run_as_root apk add --no-cache docker-cli-compose >/dev/null 2>&1
        return $?
    fi

    return 1
}

install_compose_plugin_manually() {
    ensure_fetcher

    arch=$(uname -m)
    case "$arch" in
        x86_64|amd64)
            compose_arch="x86_64"
            ;;
        aarch64|arm64)
            compose_arch="aarch64"
            ;;
        armv7l|armv7)
            compose_arch="armv7"
            ;;
        armv6l|armv6)
            compose_arch="armv6"
            ;;
        *)
            echo "Unsupported CPU architecture for Docker Compose download: $arch" >&2
            return 1
            ;;
    esac

    plugin_path=$(mktemp)
    fetch_url "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$compose_arch" "$plugin_path"

    if [ "$(id -u)" -eq 0 ] || have_passwordless_sudo; then
        run_as_root mkdir -p /usr/local/lib/docker/cli-plugins
        run_as_root cp "$plugin_path" /usr/local/lib/docker/cli-plugins/docker-compose
        run_as_root chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
    else
        user_plugin_dir="${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins"
        mkdir -p "$user_plugin_dir"
        cp "$plugin_path" "$user_plugin_dir/docker-compose"
        chmod 0755 "$user_plugin_dir/docker-compose"
    fi

    rm -f "$plugin_path"
}

ensure_compose() {
    if has_compose_plugin || has_compose_binary; then
        return 0
    fi

    install_compose_package || true
    if has_compose_plugin || has_compose_binary; then
        return 0
    fi

    install_compose_plugin_manually
    if has_compose_plugin || has_compose_binary; then
        return 0
    fi

    echo "Docker Compose is not available after automatic bootstrap." >&2
    exit 1
}

run_compose_up() {
    if docker_accessible && docker compose version >/dev/null 2>&1; then
        docker compose -f "$COMPOSE_FILE_PATH" up -d --build
        return 0
    fi

    if docker_accessible_via_sudo && sudo docker compose version >/dev/null 2>&1; then
        sudo docker compose -f "$COMPOSE_FILE_PATH" up -d --build
        return 0
    fi

    if docker_accessible && command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
        docker-compose -f "$COMPOSE_FILE_PATH" up -d --build
        return 0
    fi

    if docker_accessible_via_sudo && sudo docker-compose version >/dev/null 2>&1; then
        sudo docker-compose -f "$COMPOSE_FILE_PATH" up -d --build
        return 0
    fi

    echo "Docker is installed, but the daemon is not accessible to the deploy user." >&2
    exit 1
}

require_project_file "$COMPOSE_FILE_PATH"
require_project_file "$APP_DIR/Dockerfile"
require_project_file "$APP_DIR/requirements.txt"
require_project_file "$APP_DIR/app/main.py"

ensure_docker
ensure_docker_service
ensure_compose
run_compose_up
