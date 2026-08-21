#!/usr/bin/env bash
set -euo pipefail
set +x
export LANG=C.UTF-8 LC_ALL=C.UTF-8

if (( $# != 1 )); then
    echo "usage: $0 ADMIN_USER" >&2
    exit 2
fi
admin_user="$1"

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
! getent passwd alarm >/dev/null

actual="$(awk -F: '($3==0 || ($3>=1000 && $3<65534)) && $7 !~ /(nologin|false)$/ {print $1}' /etc/passwd | sort)"
expected="$(printf '%s\n' root "$admin_user" | sort)"
[[ "$actual" == "$expected" ]]

# The source image intentionally has no host keys. Generate them only in this
# disposable smoke-test overlay so sshd's full configuration check can run.
ssh-keygen -A
sshd -t
effective="$(sshd -T)"
grep -qx "allowusers $admin_user" <<<"$effective"
grep -qx 'passwordauthentication yes' <<<"$effective"
grep -qx 'pubkeyauthentication no' <<<"$effective"
grep -qx 'kbdinteractiveauthentication no' <<<"$effective"
grep -qx 'permitrootlogin no' <<<"$effective"
nft -c -f /etc/nftables.conf
systemctl is-enabled sshd.service systemd-networkd.service systemd-resolved.service nftables.service sshguard.service oci-grow-root.service >/dev/null

echo OCI_IMAGE_UEFI_SMOKE_SUCCESS
sync
systemctl poweroff --no-block
