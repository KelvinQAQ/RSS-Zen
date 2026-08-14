#!/usr/bin/env bash
# Bootstrap or release RSS-Zen on a single systemd Linux host.
# Bootstrap: sudo bash scripts/deploy-linux.sh bootstrap [--yes]
# Release:   sudo -u rss-zen-deploy bash /opt/rss-zen/source/scripts/deploy-linux.sh release
set -Eeuo pipefail
IFS=$'\n\t'

readonly SERVICE_USER="rss-zen"
readonly DEPLOY_USER="rss-zen-deploy"
readonly DEPLOY_GROUP="rss-zen-deploy"
readonly ETC_DIR="/etc/rss-zen"
readonly STATE_DIR="/var/lib/rss-zen"
readonly APP_ROOT="/opt/rss-zen"
readonly SOURCE_DIR="${APP_ROOT}/source"
readonly RELEASES_DIR="${APP_ROOT}/releases"
readonly CURRENT_LINK="${APP_ROOT}/current"
readonly CONFIG_PATH="${ETC_DIR}/rss-zen.toml"
readonly ENV_PATH="${ETC_DIR}/rss-zen.env"
readonly UV_BIN="/usr/local/bin/uv"
readonly MANAGED_PYTHON_DIR="${APP_ROOT}/python"
readonly RELEASE_TMP_ROOT="${APP_ROOT}/.release-tmp"
readonly SYSTEMCTL_BIN="/usr/bin/systemctl"
readonly SUDO_BIN="/usr/bin/sudo"
readonly CONTROL_BIN="/usr/local/sbin/rss-zen-deploy-control"

ASSUME_YES=0
MODE=""
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd -P)"
RELEASE_DIR=""
PREVIOUS_RELEASE=""
PLATFORM=""

log() { printf '\n==> %s\n' "$*"; }
warn() { printf '\nWARNING: %s\n' "$*" >&2; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/deploy-linux.sh bootstrap [--yes]
  sudo -u rss-zen-deploy bash /opt/rss-zen/source/scripts/deploy-linux.sh release

bootstrap is the one-time root-only system setup. It detects a supported distribution,
installs OS packages, uv and Python 3.13, creates isolated accounts/directories, deploys
systemd units and creates protected configuration templates. It does not start services
when configuration contains placeholders.

release is a non-root deployment action. It builds a locked release from the source checkout,
atomically switches /opt/rss-zen/current, then uses one narrowly-scoped sudoers rule to enable
or restart only the RSS-Zen service and timers. It cannot modify /etc, systemd units, secrets,
or install packages.
EOF
}

confirm() {
  if [[ "${ASSUME_YES}" -eq 1 ]]; then return; fi
  read -r -p "Continue? [y/N] " reply
  [[ "${reply}" =~ ^[Yy]([Ee][Ss])?$ ]] || die "deployment cancelled"
}

parse_arguments() {
  [[ $# -ge 1 ]] || { usage; exit 2; }
  MODE="$1"
  shift
  case "${MODE}" in bootstrap|release) ;; *) usage; exit 2 ;; esac
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) ASSUME_YES=1 ;;
      --help|-h) usage; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
    shift
  done
}

validate_project() {
  local required
  for required in \
    pyproject.toml uv.lock \
    deploy/linux/rss-zen.toml.example \
    deploy/systemd/rss-zen.service \
    deploy/systemd/rss-zen-export@.service \
    deploy/systemd/rss-zen-export-daily.timer \
    deploy/systemd/rss-zen-backup.service \
    deploy/systemd/rss-zen-backup.timer \
    deploy/systemd/rss-zen-delivery.service \
    deploy/systemd/rss-zen-delivery.timer \
    deploy/systemd/rss-zen-deadline.service \
    deploy/systemd/rss-zen-deadline.timer \
    deploy/systemd/rss-zen.conf \
    deploy/systemd/rss-zen.env.example \
    deploy/sudoers/rss-zen-deploy \
    deploy/sudoers/rss-zen-deploy-control; do
    [[ -f "${PROJECT_DIR}/${required}" ]] || die "missing required project file: ${required}"
  done
}

require_systemd() {
  [[ -x "${SYSTEMCTL_BIN}" ]] || die "expected systemctl at ${SYSTEMCTL_BIN}"
  command -v systemd >/dev/null 2>&1 || die "systemd is required"
  command -v systemd-analyze >/dev/null 2>&1 || die "systemd-analyze is required"
  [[ -d /run/systemd/system ]] || die "this host is not booted with systemd"
  local version
  version="$(systemd --version | awk 'NR == 1 { print $2 }')"
  [[ "${version}" =~ ^[0-9]+$ ]] || die "unable to determine systemd version"
  (( version >= 247 )) || die "systemd ${version} is too old; systemd 247+ is required for root-only credentials"
}

detect_platform() {
  [[ -r /etc/os-release ]] || die "cannot identify Linux distribution: /etc/os-release is missing"
  # shellcheck disable=SC1091
  source /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) PLATFORM="debian" ;;
    rhel|rocky|almalinux|fedora|centos) PLATFORM="rhel" ;;
    *) die "unsupported Linux distribution: ${ID:-unknown}. No system changes were made." ;;
  esac
  log "Detected ${PRETTY_NAME:-${ID}} (${PLATFORM} family)"
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "bootstrap must be run as root via sudo"
}

require_deploy_user() {
  [[ "${EUID}" -ne 0 ]] || die "release must run as ${DEPLOY_USER}, not root"
  [[ "$(id -un)" == "${DEPLOY_USER}" ]] || die "release must run as ${DEPLOY_USER}"
  [[ -x "${SUDO_BIN}" ]] || die "expected sudo at ${SUDO_BIN}"
  sudo -n -l "${CONTROL_BIN}" >/dev/null 2>&1 || die "missing scoped sudoers deployment rule"
}

install_os_dependencies() {
  log "Step 1/8: Install required operating-system packages"
  case "${PLATFORM}" in
    debian)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y ca-certificates curl gzip sqlite3 sudo tar util-linux
      ;;
    rhel)
      if command -v dnf >/dev/null 2>&1; then
        dnf install -y ca-certificates curl gzip sqlite sudo tar util-linux
      elif command -v yum >/dev/null 2>&1; then
        yum install -y ca-certificates curl gzip sqlite sudo tar util-linux
      else
        die "neither dnf nor yum is available"
      fi
      ;;
  esac
}

ensure_uv_and_python() {
  log "Step 2/8: Install or verify uv and Python 3.13"
  if [[ ! -x "${UV_BIN}" ]]; then
    if command -v uv >/dev/null 2>&1; then
      install -Dm0755 "$(command -v uv)" "${UV_BIN}"
    else
      local installer
      installer="$(mktemp)"
      curl --fail --location --proto '=https' --tlsv1.2 \
        --output "${installer}" https://astral.sh/uv/install.sh
      UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh "${installer}"
      rm -f "${installer}"
    fi
  fi
  install -d -o root -g root -m 0755 "${MANAGED_PYTHON_DIR}"
  "${UV_BIN}" --version
  UV_PYTHON_INSTALL_DIR="${MANAGED_PYTHON_DIR}" "${UV_BIN}" python install 3.13
  chmod -R a+rX "${MANAGED_PYTHON_DIR}"
}

create_accounts_and_directories() {
  log "Step 3/8: Create isolated service and deployment accounts"
  if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "${STATE_DIR}" \
      --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
  if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
    useradd --create-home --user-group --home-dir "${APP_ROOT}/deploy-home" \
      --shell /bin/bash "${DEPLOY_USER}"
  fi
  install -d -o root -g "${DEPLOY_GROUP}" -m 2775 \
    "${APP_ROOT}" "${RELEASES_DIR}" "${RELEASE_TMP_ROOT}"
  install -d -o root -g "${DEPLOY_GROUP}" -m 2770 "${SOURCE_DIR}"
  install -d -o root -g root -m 0755 "${MANAGED_PYTHON_DIR}"
  install -d -o root -g root -m 0755 "${ETC_DIR}"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 \
    "${STATE_DIR}" "${STATE_DIR}/exports" "${STATE_DIR}/backups" "${STATE_DIR}/locks"
}

copy_source_tree() {
  local source="$1"
  local destination="$2"
  tar -C "${source}" \
    --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=.ruff_cache \
    --exclude=.test-tmp --exclude=.e2e --exclude=.codewhale --exclude=__pycache__ \
    --exclude='*.pyc' --exclude=rss-zen.toml --exclude=rss-zen.yaml \
    --exclude=rss-zen.yml --exclude=data --exclude=exports \
    -cf - . | tar -C "${destination}" -xf -
}

seed_deployment_source() {
  log "Step 4/8: Seed deployment-user source checkout"
  if [[ -f "${SOURCE_DIR}/pyproject.toml" ]]; then
    log "Keeping existing ${SOURCE_DIR} source checkout"
    return
  fi
  rm -rf "${SOURCE_DIR:?}"/*
  copy_source_tree "${PROJECT_DIR}" "${SOURCE_DIR}"
  chown -R "${DEPLOY_USER}:${DEPLOY_GROUP}" "${SOURCE_DIR}"
  chmod -R u+rwX,g+rwX,o-rwx "${SOURCE_DIR}"
}

install_configuration_templates() {
  log "Step 5/8: Preserve or create production configuration"
  if [[ ! -e "${CONFIG_PATH}" ]]; then
    install -o root -g "${SERVICE_USER}" -m 0640 \
      "${PROJECT_DIR}/deploy/linux/rss-zen.toml.example" "${CONFIG_PATH}"
    warn "Created ${CONFIG_PATH}; edit provider settings and add HTTPS feeds."
  else
    log "Keeping existing ${CONFIG_PATH}"
  fi
  if [[ ! -e "${ENV_PATH}" ]]; then
    install -o root -g root -m 0600 \
      "${PROJECT_DIR}/deploy/systemd/rss-zen.env.example" "${ENV_PATH}"
    warn "Created ${ENV_PATH}; replace secret placeholders with real values."
  else
    log "Keeping existing ${ENV_PATH}"
  fi
}

install_system_files() {
  log "Step 6/8: Install systemd units, tmpfiles, and scoped sudoers"
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen.service" /etc/systemd/system/rss-zen.service
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen-export@.service" \
    /etc/systemd/system/rss-zen-export@.service
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen-export-daily.timer" \
    /etc/systemd/system/rss-zen-export-daily.timer
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen-backup.service" \
    /etc/systemd/system/rss-zen-backup.service
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen-backup.timer" \
    /etc/systemd/system/rss-zen-backup.timer
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen-delivery.service" \
    /etc/systemd/system/rss-zen-delivery.service
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen-delivery.timer" \
    /etc/systemd/system/rss-zen-delivery.timer
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen-deadline.service" \
    /etc/systemd/system/rss-zen-deadline.service
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen-deadline.timer" \
    /etc/systemd/system/rss-zen-deadline.timer
  install -m 0644 "${PROJECT_DIR}/deploy/systemd/rss-zen.conf" /etc/tmpfiles.d/rss-zen.conf
  local control_tmp
  control_tmp="$(mktemp /usr/local/sbin/rss-zen-deploy-control.XXXXXX)"
  install -o root -g root -m 0755 \
    "${PROJECT_DIR}/deploy/sudoers/rss-zen-deploy-control" "${control_tmp}"
  if ! "${control_tmp}" --self-check >/dev/null 2>&1; then
    rm -f "${control_tmp}"
    die "deployment control wrapper self-check failed; existing wrapper was preserved"
  fi
  install -o root -g root -m 0755 "${control_tmp}" "${CONTROL_BIN}"
  rm -f "${control_tmp}"
  local sudoers_tmp
  sudoers_tmp="$(mktemp /etc/sudoers.d/rss-zen-deploy.XXXXXX)"
  sed "s|@CONTROL@|${CONTROL_BIN}|g" \
    "${PROJECT_DIR}/deploy/sudoers/rss-zen-deploy" > "${sudoers_tmp}"
  chmod 0440 "${sudoers_tmp}"
  if ! visudo -cf "${sudoers_tmp}"; then
    rm -f "${sudoers_tmp}"
    die "generated sudoers file failed validation; existing sudoers was preserved"
  fi
  install -o root -g root -m 0440 "${sudoers_tmp}" /etc/sudoers.d/rss-zen-deploy
  rm -f "${sudoers_tmp}"
  systemd-tmpfiles --create /etc/tmpfiles.d/rss-zen.conf
  systemctl daemon-reload
  systemd-analyze verify \
    /etc/systemd/system/rss-zen.service \
    /etc/systemd/system/rss-zen-export@.service \
    /etc/systemd/system/rss-zen-export-daily.timer \
    /etc/systemd/system/rss-zen-backup.service \
    /etc/systemd/system/rss-zen-backup.timer \
    /etc/systemd/system/rss-zen-delivery.service \
    /etc/systemd/system/rss-zen-delivery.timer \
    /etc/systemd/system/rss-zen-deadline.service \
    /etc/systemd/system/rss-zen-deadline.timer
}

atomic_symlink() {
  local target="$1"
  local link="$2"
  local temporary="${link}.new"
  rm -f "${temporary}"
  ln -s "${target}" "${temporary}"
  mv -Tf "${temporary}" "${link}"
}

configuration_is_ready() {
  ! grep -Eqi 'replace-me|example\.invalid|your[_ -]|change[_ -]me' "${CONFIG_PATH}" "${ENV_PATH}"
}

build_release() {
  log "Build locked application release as ${DEPLOY_USER}"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  RELEASE_DIR="${RELEASES_DIR}/${stamp}"
  [[ ! -e "${RELEASE_DIR}" ]] || die "release destination already exists: ${RELEASE_DIR}"
  local staging_dir="${RELEASE_TMP_ROOT}/${stamp}"
  rm -rf "${staging_dir}"
  install -d -m 0755 "${staging_dir}"
  copy_source_tree "${PROJECT_DIR}" "${staging_dir}"
  (
    cd "${staging_dir}"
    UV_PYTHON_INSTALL_DIR="${MANAGED_PYTHON_DIR}" "${UV_BIN}" sync --locked --no-dev --python 3.13
  )
  chmod -R a+rX "${staging_dir}"
  mv -T "${staging_dir}" "${RELEASE_DIR}"
}

rollback_release() {
  local status=$?
  if [[ -n "${PREVIOUS_RELEASE}" ]]; then
    warn "Release activation failed; restoring ${PREVIOUS_RELEASE}."
    atomic_symlink "${PREVIOUS_RELEASE}" "${CURRENT_LINK}" || true
    "${SUDO_BIN}" -n "${CONTROL_BIN}" restart || true
  else
    warn "First release activation failed; removing current release link."
    rm -f "${CURRENT_LINK}"
  fi
  exit "${status}"
}

activate_release() {
  log "Atomically activate release and restart only approved units"
  if ! configuration_is_ready; then
    warn "Configuration still has placeholders. Built release was not activated."
    warn "Edit ${CONFIG_PATH} and ${ENV_PATH}, then rerun release."
    return
  fi
  PREVIOUS_RELEASE="$(readlink -f "${CURRENT_LINK}" 2>/dev/null || true)"
  trap rollback_release ERR
  atomic_symlink "${RELEASE_DIR}" "${CURRENT_LINK}"
  "${SUDO_BIN}" -n "${CONTROL_BIN}" activate
  trap - ERR
}

bootstrap() {
  require_root
  validate_project
  require_systemd
  detect_platform
  cat <<EOF

RSS-Zen root bootstrap
  Original source checkout: ${PROJECT_DIR}
  Deployment-user source checkout: ${SOURCE_DIR}
  Releases: ${RELEASES_DIR}
  Service configuration: ${CONFIG_PATH}
  Secret file: ${ENV_PATH} (root:root, mode 0600)

This one-time action installs host prerequisites and creates separated accounts.
It requires systemd 247+ so root-owned secrets can be passed through LoadCredential=.
It will not start services while configuration placeholders remain.
EOF
  confirm
  install_os_dependencies
  ensure_uv_and_python
  create_accounts_and_directories
  seed_deployment_source
  install_configuration_templates
  install_system_files
  cat <<EOF

Bootstrap completed.

1. Edit ${CONFIG_PATH} and ${ENV_PATH} as root.
2. Build and activate as the deployment user:
   sudo -u ${DEPLOY_USER} bash ${SOURCE_DIR}/scripts/deploy-linux.sh release

The deployment user can only build releases and use the scoped sudoers rule to enable/restart
RSS-Zen units. It cannot directly read ${ENV_PATH}, modify /etc, or install packages. It must still
be trusted with code publishing, because released code runs as the RSS-Zen service identity.
EOF
}

release() {
  require_deploy_user
  validate_project
  [[ -x "${UV_BIN}" ]] || die "${UV_BIN} is missing; run bootstrap as root"
  [[ -d "${MANAGED_PYTHON_DIR}" ]] || die "managed Python is missing; run bootstrap as root"
  [[ -d "${RELEASES_DIR}" && -w "${RELEASES_DIR}" ]] || die "release directory is not writable"
  build_release
  activate_release
  if configuration_is_ready; then
    "${SUDO_BIN}" -n "${CONTROL_BIN}" status || true
  fi
}

main() {
  parse_arguments "$@"
  case "${MODE}" in
    bootstrap) bootstrap ;;
    release) release ;;
  esac
}

main "$@"
