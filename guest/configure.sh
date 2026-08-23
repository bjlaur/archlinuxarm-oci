#!/usr/bin/env bash
set -euo pipefail

if (( $# != 2 )); then
    echo "usage: $0 BUILD_MODE IMAGE_USER" >&2
    exit 2
fi
build_mode="$1"
image_user="$2"
[[ "$build_mode" == development || "$build_mode" == factory ]]

pacman-key --init
pacman-key --populate archlinuxarm
packages=(
    grub efibootmgr dosfstools sudo nftables sshguard cloud-guest-utils \
    gptfdisk e2fsprogs openssh vim
)
if [[ "$build_mode" == factory ]]; then
    packages+=(cloud-init)
fi
for attempt in 1 2 3; do
    if pacman -Sy --needed --noconfirm "${packages[@]}"; then
        break
    fi
    if (( attempt == 3 )); then
        echo "package installation failed after $attempt attempts" >&2
        exit 1
    fi
    echo "package installation attempt $attempt failed; retrying..." >&2
    sleep $((attempt * 5))
done

# Arch Linux ARM's generic rootfs ships the human login account `alarm`.
# Factory images retain it; development images replace all ordinary logins.
while IFS=: read -r user _ uid _; do
    if (( uid >= 1000 && uid < 65534 )) && \
       [[ "$build_mode" != factory || "$user" != alarm ]]; then
        userdel -r "$user" 2>/dev/null || userdel "$user" || true
    fi
done < /etc/passwd

if [[ "$build_mode" == factory ]]; then
    [[ "$image_user" == alarm ]]
    getent passwd alarm >/dev/null
    usermod -s /bin/bash -aG wheel alarm
    passwd -l root
else
    if getent passwd "$image_user" >/dev/null; then
        echo "requested admin username already belongs to an existing system account: $image_user" >&2
        exit 1
    fi
    useradd -m -G wheel -s /bin/bash "$image_user"
fi
rm -rf /root/.ssh "/home/$image_user/.ssh"

ln -sf /usr/share/zoneinfo/UTC /etc/localtime
sed -i 's/^#\(en_US.UTF-8 UTF-8\)/\1/' /etc/locale.gen
locale-gen
