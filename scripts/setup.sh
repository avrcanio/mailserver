#!/usr/bin/env bash
set -euEo pipefail
shopt -s inherit_errexit 2>/dev/null || true

CONFIG_PATH=
CONTAINER_NAME=
CRI=
DEFAULT_CONFIG_PATH=
DESIRED_CONFIG_PATH=
DIR="$(pwd)"
DMS_CONFIG='/tmp/docker-mailserver'
IMAGE_NAME=
DEFAULT_IMAGE_NAME='ghcr.io/docker-mailserver/docker-mailserver:15.1.0'
INFO=
PODMAN_ROOTLESS=false
USE_SELINUX=
USE_TTY=
VOLUME=

function _show_local_usage() {
  cat <<'EOF'
OPTIONS
  Config path, container or image adjustments
    -i IMAGE_NAME
      Docker Mailserver image name. Default:
      ghcr.io/docker-mailserver/docker-mailserver:15.1.0

    -c CONTAINER_NAME
      Running container name.

    -p PATH
      Local path of the config folder to mount into a temporary container.

  SELinux
    -z
      Shared label on bind mount content.

    -Z
      Private label on bind mount content.

  Podman
    -R
      Accept rootless Podman mode.
EOF
}

function _get_absolute_script_directory() {
  if dirname "$(readlink -f "${0}")" &>/dev/null; then
    DIR="$(dirname "$(readlink -f "${0}")")"
  elif realpath -e -L "${0}" &>/dev/null; then
    DIR="$(realpath -e -L "${0}")"
    DIR="${DIR%/setup.sh}"
  fi
}

function _set_default_config_path() {
  DEFAULT_CONFIG_PATH="${DIR%/scripts}/docker-data/dms/config"
}

function _handle_config_path() {
  if [[ -z "${DESIRED_CONFIG_PATH}" ]]; then
    if [[ -n "${CONTAINER_NAME}" ]]; then
      VOLUME="$(${CRI} inspect "${CONTAINER_NAME}" \
        --format="{{range .Mounts}}{{ println .Source .Destination}}{{end}}" | \
        grep "${DMS_CONFIG}$" 2>/dev/null || :)"
    fi

    if [[ -n "${VOLUME}" ]]; then
      CONFIG_PATH="$(echo "${VOLUME}" | awk '{print $1}')"
    fi

    if [[ -z "${CONFIG_PATH}" ]]; then
      CONFIG_PATH="${DEFAULT_CONFIG_PATH}"
    fi
  else
    CONFIG_PATH="${DESIRED_CONFIG_PATH}"
  fi
}

function _run_in_new_container() {
  if ! ${CRI} history -q "${IMAGE_NAME}" &>/dev/null; then
    echo "Image '${IMAGE_NAME}' not found. Pulling ..."
    ${CRI} pull "${IMAGE_NAME}"
  fi

  ${CRI} run --rm "${USE_TTY}" \
    -v "${CONFIG_PATH}:${DMS_CONFIG}${USE_SELINUX}" \
    "${IMAGE_NAME}" setup "${@}"
}

function _main() {
  _get_absolute_script_directory
  _set_default_config_path

  local OPTIND
  while getopts ":c:i:p:zZR" opt; do
    case "${opt}" in
      i) IMAGE_NAME="${OPTARG}" ;;
      z|Z) USE_SELINUX=":${opt}" ;;
      c) CONTAINER_NAME="${OPTARG}" ;;
      R) PODMAN_ROOTLESS=true ;;
      p)
        case "${OPTARG}" in
          /*) DESIRED_CONFIG_PATH="${OPTARG}" ;;
          *) DESIRED_CONFIG_PATH="${DIR}/${OPTARG}" ;;
        esac

        if [[ ! -d "${DESIRED_CONFIG_PATH}" ]]; then
          echo "Specified directory '${DESIRED_CONFIG_PATH}' doesn't exist" >&2
          exit 1
        fi
        ;;
      *)
        echo "Invalid option: '-${OPTARG}'" >&2
        _show_local_usage
        exit 1
        ;;
    esac
  done
  shift $((OPTIND - 1))

  if command -v docker &>/dev/null; then
    CRI=docker
  elif command -v podman &>/dev/null; then
    CRI=podman
    if ! ${PODMAN_ROOTLESS} && [[ ${EUID} -ne 0 ]]; then
      read -r -p "You are running Podman in rootless mode. Continue? [Y/n] "
      [[ -n "${REPLY}" ]] && [[ "${REPLY}" =~ (n|N) ]] && exit 0
    fi
  else
    echo 'No supported Container Runtime Interface detected.'
    exit 1
  fi

  INFO="$(${CRI} ps --no-trunc --format "{{.Image}};{{.Names}}" \
    --filter label=org.opencontainers.image.title="docker-mailserver" | tail -1)"

  [[ -z "${CONTAINER_NAME}" ]] && CONTAINER_NAME="${INFO#*;}"
  [[ -z "${IMAGE_NAME}" ]] && IMAGE_NAME="${INFO%;*}"
  if [[ -z "${IMAGE_NAME}" ]]; then
    IMAGE_NAME="${DEFAULT_IMAGE_NAME}"
  fi

  if test -t 0; then
    USE_TTY="-it"
  else
    USE_TTY="-t"
  fi

  _handle_config_path

  if [[ -n "${CONTAINER_NAME}" ]]; then
    ${CRI} exec "${USE_TTY}" "${CONTAINER_NAME}" setup "${@}"
  else
    _run_in_new_container "${@}"
  fi
}

[[ -z "${1:-}" ]] && set -- help
_main "$@"
