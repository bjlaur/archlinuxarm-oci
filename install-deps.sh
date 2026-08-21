#!/usr/bin/env bash
set -euo pipefail

# Installs only dependency packages that are not already installed.
# It deliberately does not pass already-installed package names to pacman/apt.

if command -v pacman >/dev/null 2>&1; then
    packages=(
        python qemu-img qemu-user-static qemu-user-static-binfmt libarchive
        gptfdisk dosfstools e2fsprogs curl gnupg util-linux systemd
    )
    missing=()
    for pkg in "${packages[@]}"; do
        pacman -Q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if ((${#missing[@]} == 0)); then
        echo "All host dependencies are already installed."
        exit 0
    fi
    printf 'Installing missing packages only: %s\n' "${missing[*]}"
    sudo pacman -S --needed "${missing[@]}"

elif command -v apt-get >/dev/null 2>&1; then
    # Ubuntu/Debian. qemu-user-static configures the foreign-arch interpreter on
    # releases where it is a real package; binfmt-support supplies the kernel glue.
    qemu_binfmt_pkg="qemu-user-static"
    if ! apt-cache show qemu-user-static 2>/dev/null | grep -q '^Package: qemu-user-static$'; then
        qemu_binfmt_pkg="qemu-user-binfmt"
    fi
    packages=(
        python3 qemu-utils "$qemu_binfmt_pkg" binfmt-support libarchive-tools gdisk
        dosfstools e2fsprogs curl gnupg util-linux systemd udev
    )
    missing=()
    for pkg in "${packages[@]}"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'ok installed' || missing+=("$pkg")
    done
    if ((${#missing[@]} == 0)); then
        echo "All host dependencies are already installed."
        exit 0
    fi
    printf 'Installing missing packages only: %s\n' "${missing[*]}"
    sudo apt-get update
    sudo apt-get install "${missing[@]}"

else
    echo "Unsupported host package manager. See DEPENDENCIES.md for required commands." >&2
    exit 1
fi
