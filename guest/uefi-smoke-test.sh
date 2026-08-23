#!/usr/bin/env bash
set -euo pipefail
set +x
export LANG=C.UTF-8 LC_ALL=C.UTF-8

if (( $# != 3 )); then
    echo "usage: $0 BUILD_MODE IMAGE_USER SMOKE_KEY_B64" >&2
    exit 2
fi
build_mode="$1"
image_user="$2"
smoke_key_b64="$3"
[[ "$build_mode" == development || "$build_mode" == factory ]]

fail() {
    status=$?
    echo "OCI_IMAGE_UEFI_SMOKE_FAILED status=$status line=${BASH_LINENO[0]:-unknown}" >&2
    sync
    systemctl poweroff --no-block || true
    exit "$status"
}
trap fail ERR

[[ "$(uname -m)" == aarch64 ]]
[[ "$(findmnt -n -o SOURCE /)" == /dev/vda2 ]]
[[ -f /boot/efi/EFI/BOOT/BOOTAA64.EFI ]]

actual="$(awk -F: '($3==0 || ($3>=1000 && $3<65534)) && $7 !~ /(nologin|false)$/ {print $1}' /etc/passwd | sort)"
expected="$(printf '%s\n' root "$image_user" | sort)"
[[ "$actual" == "$expected" ]]
if [[ "$build_mode" == development ]]; then
    ! getent passwd alarm >/dev/null
else
    [[ "$image_user" == alarm ]]
    ! getent passwd arch >/dev/null
    [[ "$(passwd -S root | awk '{print $2}')" == L ]]
    [[ "$(passwd -S alarm | awk '{print $2}')" != L ]]
    # cloud-final.service has already completed because the smoke unit Requires
    # and orders itself after it. Do not use --wait here: cloud-init's status
    # waiter can block when this service is the explicitly selected boot target.
    cloud-init status --long
    [[ "$(cloud-id)" == nocloud ]]
    [[ -f /var/lib/archlinuxarm-oci/cloud-init-smoke ]]
    [[ "$(</proc/sys/kernel/hostname)" == oci-factory-smoke ]]
    expected_key="$(printf '%s' "$smoke_key_b64" | base64 -d)"
    grep -Fxq "$expected_key" /home/alarm/.ssh/authorized_keys
    sudo -u alarm sudo -n true
fi

# The source image intentionally has no host keys. Generate them only in this
# disposable smoke-test overlay so sshd's full configuration check can run.
ssh-keygen -A
sshd -t
effective="$(sshd -T)"
grep -Fqxi "allowusers $image_user" <<<"$effective"
if [[ "$build_mode" == factory ]]; then
    grep -Fqxi 'passwordauthentication no' <<<"$effective"
    grep -Fqxi 'pubkeyauthentication yes' <<<"$effective"
else
    grep -Fqxi 'passwordauthentication yes' <<<"$effective"
    grep -Fqxi 'pubkeyauthentication no' <<<"$effective"
fi
grep -Fqxi 'kbdinteractiveauthentication no' <<<"$effective"
grep -Fqxi 'permitrootlogin no' <<<"$effective"
nft -c -f /etc/nftables.conf
systemctl is-enabled sshd.service systemd-networkd.service systemd-resolved.service nftables.service sshguard.service oci-grow-root.service >/dev/null

echo OCI_IMAGE_UEFI_SMOKE_SUCCESS
sync
systemctl poweroff --no-block
