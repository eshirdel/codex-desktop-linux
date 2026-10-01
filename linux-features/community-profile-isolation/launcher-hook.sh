#!/usr/bin/env bash
set -Eeuo pipefail

: "${HOME:?HOME is required}"
: "${CODEX_LINUX_APP_DIR:?CODEX_LINUX_APP_DIR is required}"

CODEX_COMMUNITY_CODEX_HOME_SPEC='~/.codex-community'
CODEX_COMMUNITY_ELECTRON_USER_DATA_SPEC='~/.config/Codex-Community'
profile_config="$CODEX_LINUX_APP_DIR/.codex-linux/features/community-profile-isolation/profile-config.sh"
if [[ -r "$profile_config" ]]; then
    # shellcheck source=/dev/null
    . "$profile_config"
fi

codex_community_expand_profile_path() {
    local spec="$1"
    case "$spec" in
        "~") printf '%s\n' "$HOME" ;;
        "~/"*) printf '%s/%s\n' "$HOME" "${spec#\~/}" ;;
        /*) printf '%s\n' "$spec" ;;
        *) return 64 ;;
    esac
}

canonical="$(codex_community_expand_profile_path "$CODEX_COMMUNITY_ELECTRON_USER_DATA_SPEC")" || {
    printf 'Invalid Community Electron profile path spec: %s\n' "$CODEX_COMMUNITY_ELECTRON_USER_DATA_SPEC" >&2
    exit 64
}
wrapper="$CODEX_LINUX_APP_DIR/.codex-linux/features/community-profile-isolation/codex-cli-wrapper.sh"

if [[ -n "${CODEX_ELECTRON_USER_DATA_PATH:-}" && "$CODEX_ELECTRON_USER_DATA_PATH" != "$canonical" ]]; then
  printf "ChatGPT Community refuses non-canonical CODEX_ELECTRON_USER_DATA_PATH: %s\n" \
    "$CODEX_ELECTRON_USER_DATA_PATH" >&2
  exit 64
fi

for arg in "$@"; do
  case "$arg" in
    --user-data-dir="$canonical") ;;
    --user-data-dir|--user-data-dir=*)
      printf "ChatGPT Community refuses non-canonical --user-data-dir: %s\n" "$arg" >&2
      exit 64
      ;;
  esac
done

if [[ ! -x "$wrapper" ]]; then
  printf "ChatGPT Community CLI isolation wrapper is missing or not executable: %s\n" \
    "$wrapper" >&2
  exit 69
fi

printf "env CODEX_ELECTRON_USER_DATA_PATH=%s\n" "$canonical"
printf "env CODEX_CLI_PATH=%s\n" "$wrapper"
printf "electron-arg %s\n" "--user-data-dir=$canonical"
