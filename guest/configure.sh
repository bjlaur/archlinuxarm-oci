#!/usr/bin/env bash
set -euo pipefail

pacman-key --init
pacman-key --populate archlinuxarm
pacman -Syu --noconfirm
pacman -S --needed --noconfirm \
    grub efibootmgr dosfstools sudo nftables sshguard cloud-guest-utils \
    gptfdisk e2fsprogs openssh vim

# Arch Linux ARM's generic rootfs ships the human login account `alarm`.
# Remove every ordinary UID login account before creating the only intended one.
while IFS=: read -r user _ uid _; do
    if (( uid >= 1000 && uid < 65534 )); then
        userdel -r "$user" 2>/dev/null || userdel "$user" || true
    fi
done < /etc/passwd

useradd -m -G wheel -s /bin/bash nullstring
rm -rf /root/.ssh /home/nullstring/.ssh

ln -sf /usr/share/zoneinfo/UTC /etc/localtime
sed -i 's/^#\(en_US.UTF-8 UTF-8\)/\1/' /etc/locale.gen
locale-gen
