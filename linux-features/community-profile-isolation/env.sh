# Community package hard isolation.
# This hook is sourced with `set -a` by the packaged launcher.
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

if ! CODEX_HOME="$(codex_community_expand_profile_path "$CODEX_COMMUNITY_CODEX_HOME_SPEC")"; then
    printf 'Invalid Community CODEX_HOME path spec: %s\n' "$CODEX_COMMUNITY_CODEX_HOME_SPEC" >&2
    return 64
fi
unset profile_config CODEX_COMMUNITY_CODEX_HOME_SPEC CODEX_COMMUNITY_ELECTRON_USER_DATA_SPEC
unset -f codex_community_expand_profile_path 2>/dev/null || true

# Keep bare `codex` invocations launched by ChatGPT Community inside the
# Community distribution. This PATH change is process-local.
CODEX_COMMUNITY_BIN_DIR="$CODEX_LINUX_APP_DIR/resources"
case ":${PATH:-}:" in
  ":$CODEX_COMMUNITY_BIN_DIR:"*) ;;
  *)
    PATH="$CODEX_COMMUNITY_BIN_DIR${PATH:+:$PATH}"
    export PATH
    ;;
esac
unset CODEX_COMMUNITY_BIN_DIR
