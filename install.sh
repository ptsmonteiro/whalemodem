#!/bin/sh
set -eu

REPOSITORY=${WHALE_REPOSITORY:-ptsmonteiro/whalemodem}
INSTALL_ROOT=${WHALE_INSTALL_ROOT:-"$HOME/.local/share/whale"}
BIN_DIR=${WHALE_BIN_DIR:-"$HOME/.local/bin"}

platform_tag() {
    os=${1:-$(uname -s)}
    arch=${2:-$(uname -m)}
    case "$os:$arch" in
        Linux:x86_64|Linux:amd64) echo linux-x86_64 ;;
        Linux:aarch64|Linux:arm64) echo linux-aarch64 ;;
        Linux:armv7l|Linux:armv7) echo linux-armv7 ;;
        Darwin:x86_64|Darwin:amd64) echo macos-x86_64 ;;
        Darwin:arm64|Darwin:aarch64) echo macos-arm64 ;;
        *) echo "Unsupported platform: $os $arch" >&2; return 1 ;;
    esac
}

asset_name() {
    case "$1" in
        linux-*) echo "whale-$1.tar.gz" ;;
        macos-*) echo "whale-$1.zip" ;;
        *) return 1 ;;
    esac
}

release_url() {
    printf 'https://github.com/%s/releases/download/%s/%s\n' "$1" "$2" "$3"
}

latest_version() {
    curl -fsSL -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/$REPOSITORY/releases/latest" |
        sed -n 's/^[[:space:]]*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' |
        head -n 1
}

verify_checksum() {
    expected=$(awk -v name="$2" '$2 == name || $2 == "*" name { print $1; exit }' "$3")
    [ -n "$expected" ] || { echo "No SHA-256 checksum published for $2" >&2; return 1; }
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$1" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
        actual=$(shasum -a 256 "$1" | awk '{print $1}')
    else
        echo "A SHA-256 tool (sha256sum or shasum) is required." >&2; return 1
    fi
    [ "$actual" = "$expected" ] || { echo "SHA-256 verification failed for $2" >&2; return 1; }
}

add_path_to_profile() {
    case "${SHELL:-}" in
        */zsh) profile="$HOME/.zprofile" ;;
        */bash) profile="$HOME/.bashrc" ;;
        *) profile="$HOME/.profile" ;;
    esac
    line='export PATH="$HOME/.local/bin:$PATH"'
    touch "$profile"
    grep -Fqx "$line" "$profile" || printf '\n%s\n' "$line" >> "$profile"
}

main() {
    tag=$(platform_tag)
    asset=$(asset_name "$tag")
    version=$(latest_version)
    [ -n "$version" ] || { echo "Could not determine the latest Whale release." >&2; exit 1; }
    case "$version" in *[!A-Za-z0-9._+-]*) echo "Invalid release tag: $version" >&2; exit 1;; esac

    mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/whale-install.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT HUP INT TERM
    curl -fL "$(release_url "$REPOSITORY" "$version" "$asset")" -o "$tmp/$asset"
    curl -fL "$(release_url "$REPOSITORY" "$version" SHA256SUMS)" -o "$tmp/SHA256SUMS"
    verify_checksum "$tmp/$asset" "$asset" "$tmp/SHA256SUMS"

    destination="$INSTALL_ROOT/$version"
    if [ -e "$destination" ] && [ ! -d "$destination/whale" ]; then
        echo "$destination exists but is not a valid Whale installation; refusing to overwrite it." >&2
        exit 1
    fi
    if [ ! -d "$destination/whale" ]; then
        staging="$INSTALL_ROOT/.install-$version-$$"
        rm -rf "$staging"
        mkdir -p "$staging"
        case "$asset" in
            *.tar.gz) tar -xzf "$tmp/$asset" -C "$staging" ;;
            *.zip) command -v unzip >/dev/null 2>&1 || { echo "unzip is required." >&2; exit 1; }
                   unzip -q "$tmp/$asset" -d "$staging" ;;
        esac
        [ -x "$staging/whale/whale-server" ] && [ -x "$staging/whale/whale-configure" ] &&
        [ -x "$staging/whale/whale-test" ] || {
            echo "Release archive does not contain the expected Whale commands." >&2; exit 1;
        }
        mv "$staging" "$destination"
    fi

    if [ -e "$INSTALL_ROOT/current" ] && [ ! -L "$INSTALL_ROOT/current" ]; then
        echo "$INSTALL_ROOT/current exists and is not an installer-managed link." >&2
        exit 1
    fi
    ln -sfn "$destination" "$INSTALL_ROOT/current"
    ln -sfn "$INSTALL_ROOT/current/whale/whale-server" "$BIN_DIR/whale-server"
    ln -sfn "$INSTALL_ROOT/current/whale/whale-configure" "$BIN_DIR/whale-configure"
    ln -sfn "$INSTALL_ROOT/current/whale/whale-test" "$BIN_DIR/whale-test"
    add_path_to_profile
    echo "Whale $version installed. Existing config.toml files were left unchanged."
    echo "Open a new terminal (or reload your shell profile) before running whale-server."
}

[ "${WHALE_INSTALLER_TEST:-0}" = 1 ] || main "$@"
