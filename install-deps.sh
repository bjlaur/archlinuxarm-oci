#!/usr/bin/env bash
set -euo pipefail

# Installs only dependency packages that are not already installed.
# It deliberately does not pass already-installed package names to pacman/apt.

if command -v pacman >/dev/null 2>&1; then
    packages=(
        python qemu-img qemu-system-aarch64 edk2-aarch64 libguestfs curl gnupg
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
    packages=(
        python3 qemu-utils qemu-system-arm qemu-efi-aarch64 libguestfs-tools curl gnupg
    )
    # supermin builds the libguestfs appliance from an installed host kernel.
    # Minimal ARM64 environments (including GitHub's hosted ARM runner) may run
    # an Azure kernel without providing a kernel image package for supermin.
    case "$(uname -m)" in
        aarch64|arm64) packages+=(linux-image-arm64) ;;
    esac
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
