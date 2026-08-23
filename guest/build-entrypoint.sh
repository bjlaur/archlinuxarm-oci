#!/usr/bin/env bash
set -euo pipefail
set +x
export LANG=C.UTF-8 LC_ALL=C.UTF-8

if (( $# != 2 )); then
    echo "usage: $0 BUILD_MODE IMAGE_USER" >&2
    exit 2
fi
build_mode="$1"
image_user="$2"
[[ "$build_mode" == development || "$build_mode" == factory ]]
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
[[ "$(cat /sys/block/vda/serial)" == OCIARCHBUILDER ]]

"$builder/configure.sh" "$build_mode" "$image_user"

# Package-owned configuration is deliberately applied only after pacman has
# installed those packages, avoiding "exists in filesystem" conflicts.
cp -a "$builder/final-root/." /

if [[ "$build_mode" == development ]]; then
    echo
    echo "==> SET A NEW ROOT PASSWORD (console only; root SSH is disabled)"
    passwd root
    echo
    echo "==> SET THE SSH/SUDO PASSWORD FOR $image_user"
    passwd "$image_user"
fi

actual="$($builder/verify-login-users.sh)"
expected="$(printf '%s\n' root "$image_user" | sort)"
[[ "$actual" == "$expected" ]]
if [[ "$build_mode" == development ]]; then
    ! getent passwd alarm >/dev/null
else
    [[ "$image_user" == alarm ]]
    [[ "$(passwd -S root | awk '{print $2}')" == L ]]
    [[ "$(passwd -S alarm | awk '{print $2}')" != L ]]
    [[ "$(id -u alarm)" == 1000 ]]
    id -nG alarm | tr ' ' '\n' | grep -Fxq wheel
    [[ "$(getent passwd alarm | cut -d: -f7)" == /bin/bash ]]
    [[ ! -e /root/.ssh/authorized_keys ]]
    [[ ! -e /home/alarm/.ssh/authorized_keys ]]
    visudo -cf /etc/sudoers
    rm -rf /var/lib/cloud/*
    rm -f /var/log/cloud-init*.log
fi

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

truncate -s 0 /etc/machine-id
rm -f /var/lib/systemd/random-seed /etc/ssh/ssh_host_*
rm -f /etc/systemd/system/oci-image-build.service
rm -rf "$builder"

install -d -m 0700 /var/lib/archlinuxarm-oci
printf '%s\n' OCI_IMAGE_BUILD_SUCCESS > /var/lib/archlinuxarm-oci/build-success
sync
echo OCI_IMAGE_BUILD_SUCCESS
systemctl poweroff --no-block
