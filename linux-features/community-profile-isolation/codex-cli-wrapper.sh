#!/usr/bin/env bash
set -Eeuo pipefail

: "${HOME:?HOME is required}"
: "${CODEX_LINUX_APP_DIR:?CODEX_LINUX_APP_DIR is required}"

CODEX_COMMUNITY_CODEX_HOME_SPEC='~/.codex-community'
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

canonicalize_existing_dir() {
    local target="$1"
    if [[ -d "$target" ]]; then
        (CDPATH="" cd -P -- "$target" && pwd -P)
    else
        printf "%s\n" "$target"
    fi
}

configured_codex_home="$(codex_community_expand_profile_path "$CODEX_COMMUNITY_CODEX_HOME_SPEC")" || {
    printf 'Invalid Community CODEX_HOME path spec: %s\n' "$CODEX_COMMUNITY_CODEX_HOME_SPEC" >&2
    exit 64
}
expected_codex_home="$(canonicalize_existing_dir "$configured_codex_home")"
actual_codex_home="${CODEX_HOME:-}"
if [[ -n "$actual_codex_home" ]]; then
    actual_codex_home="$(canonicalize_existing_dir "$actual_codex_home")"
fi

if [[ "$actual_codex_home" != "$expected_codex_home" ]]; then
    printf "ChatGPT Community CLI wrapper refuses CODEX_HOME=%s; expected %s\n" \
        "${CODEX_HOME:-<unset>}" "$expected_codex_home" >&2
    exit 64
fi

community_bin="$CODEX_LINUX_APP_DIR/resources"
real_codex="$community_bin/codex"

if [[ ! -x "$real_codex" ]]; then
    printf "ChatGPT Community CLI wrapper cannot execute Community Codex: %s\n" \
        "$real_codex" >&2
    exit 69
fi

case "${PATH:-}" in
    "$community_bin"|"$community_bin":*) ;;
    *) PATH="$community_bin${PATH:+:$PATH}" ;;
esac
export PATH

exec "$real_codex" \
    -c "shell_environment_policy.set.PATH=$PATH" \
    "$@"
