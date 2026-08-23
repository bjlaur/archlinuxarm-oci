#!/usr/bin/env bash
set -euo pipefail

# Build a generic initramfs instead of hardware-autodetecting the x86 build host.
sed -i 's/^MODULES=.*/MODULES=()/' /etc/mkinitcpio.conf
sed -i 's/^HOOKS=.*/HOOKS=(base udev modconf block filesystems fsck)/' /etc/mkinitcpio.conf
mkinitcpio -P

# Offline image build: write the standard ARM64 UEFI fallback loader and do not touch NVRAM.
grub-install \
    --target=arm64-efi \
    --efi-directory=/boot/efi \
    --boot-directory=/boot \
    --bootloader-id=GRUB \
    --removable \
    --no-nvram \
    --recheck

systemctl enable \
    sshd.service \
    systemd-networkd.service \
    systemd-resolved.service \
    sshguard.service \
    serial-getty@ttyAMA0.service \
    oci-grow-root.service

for unit in \
    cloud-init-local.service \
    cloud-init-main.service \
    cloud-config.service \
    cloud-final.service; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
        systemctl enable "$unit"
    fi
done
