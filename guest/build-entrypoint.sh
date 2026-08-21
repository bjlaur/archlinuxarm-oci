#!/usr/bin/env bash
set -euo pipefail
set +x
export LANG=C.UTF-8 LC_ALL=C.UTF-8

if (( $# != 1 )); then
    echo "usage: $0 ADMIN_USER" >&2
    exit 2
fi
admin_user="$1"
builder=/usr/local/lib/archlinuxarm-oci-builder

fail() {
    status=$?
    echo "OCI_IMAGE_BUILD_FAILED status=$status line=${BASH_LINENO[0]:-unknown}" >&2
    sync
    systemctl poweroff --no-block || true
    exit "$status"
}
trap fail ERR

[[ "$(uname -m)" == aarch64 ]]
[[ "$(findmnt -n -o SOURCE /)" == /dev/vda2 ]]
[[ "$(cat /sys/block/vda/device/serial)" == OCIARCHBUILDER ]]

"$builder/configure.sh" "$admin_user"

# Package-owned configuration is deliberately applied only after pacman has
# installed those packages, avoiding "exists in filesystem" conflicts.
cp -a "$builder/final-root/." /

echo
echo "==> SET A NEW ROOT PASSWORD (console only; root SSH is disabled)"
passwd root
echo
echo "==> SET THE SSH/SUDO PASSWORD FOR $admin_user"
passwd "$admin_user"

actual="$($builder/verify-login-users.sh)"
expected="$(printf '%s\n' root "$admin_user" | sort)"
[[ "$actual" == "$expected" ]]
! getent passwd alarm >/dev/null

"$builder/finalize.sh"

for required in \
    /boot/Image \
    /boot/initramfs-linux.img \
    /boot/efi/EFI/BOOT/BOOTAA64.EFI \
    /etc/ssh/sshd_config.d/10-oci-security.conf \
    /usr/local/sbin/oci-grow-root; do
    [[ -e "$required" ]]
done

ssh-keygen -A
sshd -t
nft -c -f /etc/nftables.conf

truncate -s 0 /etc/machine-id
rm -f /var/lib/systemd/random-seed /etc/ssh/ssh_host_*
rm -f /etc/systemd/system/oci-image-build.service
rm -rf "$builder"

install -d -m 0700 /var/lib/archlinuxarm-oci
printf '%s\n' OCI_IMAGE_BUILD_SUCCESS > /var/lib/archlinuxarm-oci/build-success
sync
echo OCI_IMAGE_BUILD_SUCCESS
systemctl poweroff --no-block
