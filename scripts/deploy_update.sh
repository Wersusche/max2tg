#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(dirname "$SCRIPT_DIR")

remote="${DEPLOY_REMOTE:-origin}"
branch="${DEPLOY_BRANCH:-}"
always_deploy="false"
skip_pull="false"
run_setup="false"

usage() {
    cat <<'USAGE'
Usage:
  bash scripts/deploy_update.sh [options]

Options:
  --remote NAME          Git remote to pull from (default: origin).
  --branch NAME          Branch to deploy (default: current branch).
  --always-deploy        Rebuild/restart even when git has no new commit.
  --skip-pull            Do not fetch or pull; deploy the current checkout.
  --setup                Run scripts/setup_bridge.sh after updating.
  -h, --help             Show this help.

Environment:
  DEPLOY_REMOTE          Same as --remote.
  DEPLOY_BRANCH          Same as --branch.

Examples:
  bash scripts/deploy_update.sh
  bash scripts/deploy_update.sh --branch main --always-deploy
USAGE
}

log() {
    printf '[deploy] %s\n' "$*" >&2
}

fail() {
    printf '[deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command is missing: $1"
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --remote) remote="${2:-}"; shift 2 ;;
            --branch) branch="${2:-}"; shift 2 ;;
            --always-deploy) always_deploy="true"; shift ;;
            --skip-pull) skip_pull="true"; shift ;;
            --setup) run_setup="true"; shift ;;
            -h|--help) usage; exit 0 ;;
            *) fail "Unknown option: $1" ;;
        esac
    done
}

ensure_git_repo() {
    need_command git
    git -C "$APP_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || fail "$APP_DIR is not a git repository."
}

ensure_clean_tracked_files() {
    if ! git diff --quiet || ! git diff --cached --quiet; then
        fail "Tracked files have local changes. Commit/stash them before auto deploy."
    fi
}

current_revision() {
    git rev-parse HEAD
}

resolve_branch() {
    if [ -n "$branch" ]; then
        printf '%s\n' "$branch"
        return 0
    fi

    local current_branch
    current_branch="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$current_branch" != "HEAD" ]; then
        printf '%s\n' "$current_branch"
        return 0
    fi

    local remote_head
    remote_head="$(git symbolic-ref --quiet --short "refs/remotes/$remote/HEAD" 2>/dev/null || true)"
    if [ -n "$remote_head" ]; then
        printf '%s\n' "${remote_head#"$remote/"}"
        return 0
    fi

    fail "Cannot determine branch from detached HEAD. Pass --branch NAME."
}

checkout_branch() {
    local target_branch="$1"
    local current_branch
    current_branch="$(git rev-parse --abbrev-ref HEAD)"

    if [ "$current_branch" = "$target_branch" ]; then
        return 0
    fi

    if git show-ref --verify --quiet "refs/heads/$target_branch"; then
        log "Switching to local branch $target_branch"
        git checkout "$target_branch"
        return 0
    fi

    if git show-ref --verify --quiet "refs/remotes/$remote/$target_branch"; then
        log "Creating local branch $target_branch from $remote/$target_branch"
        git checkout -b "$target_branch" --track "$remote/$target_branch"
        return 0
    fi

    fail "Branch $target_branch was not found locally or at $remote/$target_branch."
}

pull_updates() {
    [ -n "$remote" ] || fail "Git remote name is empty."
    git remote get-url "$remote" >/dev/null 2>&1 || fail "Git remote does not exist: $remote"

    local target_branch
    target_branch="$(resolve_branch)"

    log "Fetching $remote"
    git fetch --prune "$remote"

    checkout_branch "$target_branch"

    log "Pulling $remote/$target_branch with --ff-only"
    git pull --ff-only "$remote" "$target_branch"
}

run_deploy() {
    if [ "$run_setup" = "true" ]; then
        log "Running setup_bridge.sh"
        bash "$APP_DIR/scripts/setup_bridge.sh"
        return 0
    fi

    if [ ! -f "$APP_DIR/.env" ]; then
        fail "Missing .env. Run bash scripts/setup_bridge.sh once, or rerun this script with --setup."
    fi

    log "Rebuilding and restarting bridge"
    sh "$APP_DIR/scripts/bootstrap_remote.sh"
}

main() {
    parse_args "$@"
    ensure_git_repo
    cd "$APP_DIR"

    local before_rev
    local after_rev
    local should_deploy="false"

    before_rev="$(current_revision)"

    if [ "$skip_pull" = "true" ]; then
        log "Skipping git pull; deploying current checkout"
        should_deploy="true"
    else
        ensure_clean_tracked_files
        pull_updates
        after_rev="$(current_revision)"
        if [ "$before_rev" != "$after_rev" ]; then
            log "Updated ${before_rev:0:12} -> ${after_rev:0:12}"
            should_deploy="true"
        else
            log "Already up to date at ${after_rev:0:12}"
        fi
    fi

    if [ "$always_deploy" = "true" ] || [ "$run_setup" = "true" ]; then
        should_deploy="true"
    fi

    if [ "$should_deploy" = "true" ]; then
        run_deploy
        log "Done."
    else
        log "No deploy needed. Pass --always-deploy to rebuild anyway."
    fi
}

main "$@"
