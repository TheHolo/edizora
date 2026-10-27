#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="${SERVICE_NAME:-edizora}"
APP_USER="${APP_USER:-edizora}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
APP_ROOT="${APP_ROOT:-/opt/edizora}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
DATABASE_FILE="${PROJECT_DIR}/data/database.db"
BACKUP_DIR="${PROJECT_DIR}/data/backups"
LOCK_FILE="/run/lock/${SERVICE_NAME}-update.lock"

service_stopped=0

log() {
    printf '[deploy] %s\n' "$*"
}

fail() {
    printf '[deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

as_app() {
    sudo -H -u "${APP_USER}" -- "$@"
}

on_error() {
    local exit_code=$?
    printf '[deploy] Update failed with exit code %s.\n' "${exit_code}" >&2
    if (( service_stopped )); then
        log "Attempting to start ${SERVICE_NAME} after the failed update"
        systemctl start "${SERVICE_NAME}" || true
    fi
    journalctl -u "${SERVICE_NAME}" -n 30 --no-pager || true
    exit "${exit_code}"
}

trap on_error ERR

[[ ${EUID} -eq 0 ]] || fail "Run this script with sudo: sudo ./deploy/update.sh"
id "${APP_USER}" >/dev/null 2>&1 || fail "System user ${APP_USER} does not exist"
systemctl cat "${SERVICE_NAME}" >/dev/null 2>&1 || fail "Service ${SERVICE_NAME} is not installed"
[[ -x "${VENV_PYTHON}" ]] || fail "Virtual environment not found: ${VENV_PYTHON}"
[[ -d "${PROJECT_DIR}/.git" ]] || fail "Git repository not found: ${PROJECT_DIR}"
[[ "${PROJECT_DIR}" == "${APP_ROOT}" ]] \
    || fail "Repository must be installed directly in ${APP_ROOT}; found ${PROJECT_DIR}"

exec 9>"${LOCK_FILE}"
flock -n 9 || fail "Another update is already running"

cd "${PROJECT_DIR}"

branch="$(as_app git branch --show-current)"
[[ -n "${branch}" ]] || fail "Detached HEAD is not supported"
[[ "${branch}" == "${DEPLOY_BRANCH}" ]] \
    || fail "Expected branch ${DEPLOY_BRANCH}, but repository is on ${branch}"

dirty="$(as_app git status --porcelain --untracked-files=no)"
[[ -z "${dirty}" ]] || fail "Tracked files have local changes. Commit or discard them before updating."

log "Fetching ${GIT_REMOTE}/${DEPLOY_BRANCH}"
as_app git fetch "${GIT_REMOTE}" "${DEPLOY_BRANCH}"
as_app git merge-base --is-ancestor HEAD "${GIT_REMOTE}/${DEPLOY_BRANCH}" \
    || fail "The remote branch cannot be applied as a fast-forward update"

log "Stopping ${SERVICE_NAME}"
systemctl stop "${SERVICE_NAME}"
service_stopped=1

if [[ -f "${DATABASE_FILE}" ]]; then
    install -d -o "${APP_USER}" -g "${APP_USER}" -m 750 "${BACKUP_DIR}"
    backup_file="${BACKUP_DIR}/database-$(date -u +%Y%m%d-%H%M%S).db"
    cp --preserve=mode,timestamps "${DATABASE_FILE}" "${backup_file}"
    chown "${APP_USER}:${APP_USER}" "${backup_file}"
    log "Database backup created: ${backup_file}"
fi

log "Updating ${DEPLOY_BRANCH}"
as_app git merge --ff-only "${GIT_REMOTE}/${DEPLOY_BRANCH}"

log "Installing project dependencies"
as_app "${VENV_PYTHON}" -m pip install -e "${PROJECT_DIR}"

log "Checking Python sources"
as_app "${VENV_PYTHON}" -m compileall -q "${PROJECT_DIR}/app"

log "Starting ${SERVICE_NAME}"
restart_count_before="$(systemctl show "${SERVICE_NAME}" --property=NRestarts --value)"
systemctl start "${SERVICE_NAME}"
service_stopped=0

sleep 8
systemctl is-active --quiet "${SERVICE_NAME}" \
    || fail "Service did not become active after the update"
restart_count_after="$(systemctl show "${SERVICE_NAME}" --property=NRestarts --value)"
[[ "${restart_count_after}" == "${restart_count_before}" ]] \
    || fail "Service restarted during the post-update health check"

log "Update completed successfully"
systemctl status "${SERVICE_NAME}" --no-pager --lines=10
