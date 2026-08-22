#!/usr/bin/env bash
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP must be set by GitHub Actions}"
: "${GITHUB_ENV:?GITHUB_ENV must be set by GitHub Actions}"

kernel_version="$(uname -r)"
runner_kernel="/boot/vmlinuz-$kernel_version"
kernel="$RUNNER_TEMP/libguestfs-vmlinuz-$kernel_version"
modules="/lib/modules/$kernel_version"

if [[ ! -f "$runner_kernel" ]]; then
    echo "The running kernel image is missing: $runner_kernel" >&2
    exit 1
fi
if [[ ! -d "$modules" ]]; then
    echo "Kernel modules are missing for $kernel_version: $modules" >&2
    exit 1
fi

# Azure's boot kernel is not readable by the unprivileged runner account.
# Make one readable copy; the builder and libguestfs remain unprivileged.
sudo install -m 0644 "$runner_kernel" "$kernel"
{
    echo "SUPERMIN_KERNEL=$kernel"
    echo "SUPERMIN_KERNEL_VERSION=$kernel_version"
    echo "SUPERMIN_MODULES=$modules"
} >> "$GITHUB_ENV"

echo "libguestfs appliance kernel: $kernel_version"
