#!/usr/bin/env bash
set -euo pipefail

if (( $# != 1 )); then
    echo "usage: $0 ADMIN_USER" >&2
    exit 2
fi
admin_user="$1"

pacman-key --init
pacman-key --populate archlinuxarm
pacman -Sy --needed --noconfirm \
    grub efibootmgr dosfstools sudo nftables sshguard cloud-guest-utils \
    gptfdisk e2fsprogs openssh vim

# Arch Linux ARM's generic rootfs ships the human login account `alarm`.
# Remove every ordinary UID login account before creating the only intended one.
while IFS=: read -r user _ uid _; do
    if (( uid >= 1000 && uid < 65534 )); then
        userdel -r "$user" 2>/dev/null || userdel "$user" || true
    fi
done < /etc/passwd

if getent passwd "$admin_user" >/dev/null; then
    echo "requested admin username already belongs to an existing system account: $admin_user" >&2
    exit 1
fi

useradd -m -G wheel -s /bin/bash "$admin_user"
rm -rf /root/.ssh "/home/$admin_user/.ssh"

ln -sf /usr/share/zoneinfo/UTC /etc/localtime
sed -i 's/^#\(en_US.UTF-8 UTF-8\)/\1/' /etc/locale.gen
locale-gen
