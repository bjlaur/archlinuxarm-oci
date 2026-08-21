# Codex handoff: archlinuxarm-oci

## Goal

This repository builds a bootable Arch Linux ARM AArch64 QCOW2 custom image for Oracle Cloud Infrastructure Ampere A1 (`VM.Standard.A1.Flex`). The user is building on CachyOS/Arch x86_64.

The previous OCI Arch Linux ARM server was compromised; the important known issue was the stock Arch Linux ARM `alarm` user. The current build must never expose the stock `alarm/alarm` login on first boot.

## Non-negotiable access/security decisions

Do not silently change these without discussing them with the user:

- Delete `alarm` and all other ordinary UID >=1000 login accounts before first boot.
- Keep `root`, and prompt interactively for a new root password.
- Create exactly one normal administrative login: `nullstring`.
- Prompt interactively for the `nullstring` password.
- Put `nullstring` in `wheel`; sudo must require a password.
- SSH password authentication is intentionally **enabled**.
- SSH public-key authentication is intentionally **disabled**.
- Root SSH is **disabled** (`PermitRootLogin no`).
- `AllowUsers nullstring` restricts sshd to that account.
- System/service accounts are expected to remain; "only root + nullstring" means only interactive human-login accounts.
- nftables is default-deny inbound and initially permits DHCP/ICMP/SSH.
- SSHGuard is enabled with its nftables backend.

## Design principle

Keep `build.py` as orchestration, not as a bag of embedded config strings.

Static guest files belong in `overlay/` at their final filesystem paths. Guest-side command sequences belong in `guest/*.sh`. Only values genuinely unknown until disk creation (currently filesystem UUIDs) belong in `templates/`.

This structure is deliberate because the user wants the build easy to audit and troubleshoot.

## Build flow

1. Verify host commands and root privileges.
2. Restart `systemd-binfmt.service`; require `/proc/sys/fs/binfmt_misc/qemu-aarch64`.
3. Download the official `ArchLinuxARM-aarch64-latest.tar.gz` plus `.sig`.
4. Import the published Arch Linux ARM build-system key and verify the tarball signature.
5. Create a sparse 10G raw GPT disk:
   - p1: 512 MiB EFI System Partition, FAT32.
   - p2: remaining disk, ext4 root.
6. Attach with loopback and mount both filesystems.
7. Extract the official rootfs.
8. Mount `/dev`, `/proc`, `/sys`, and `/run` for the cross-architecture chroot.
9. Execute `guest/configure.sh` under AArch64 binfmt:
   - initialize/populate ALARM pacman keys;
   - full `pacman -Syu`;
   - install guest packages;
   - remove normal login users;
   - create `nullstring`;
   - locale/timezone setup.
10. Run interactive `passwd root` and `passwd nullstring` from `build.py`.
11. Verify the only interactive users are exactly `nullstring` and `root`.
12. Copy `overlay/` into the image.
13. Render `templates/fstab` and `templates/grub.cfg` with actual UUIDs.
14. Run `guest/finalize.sh`:
   - generic mkinitcpio;
   - ARM64 UEFI GRUB at `/EFI/BOOT/BOOTAA64.EFI`;
   - enable network, sshd, firewall, SSHGuard, serial console, grow-root service.
15. Validate kernel/initramfs/EFI loader and `sshd -t`.
16. Reset machine identity and delete pre-generated SSH host keys so first boot creates fresh ones.
17. Unmount and convert raw -> compressed QCOW2 with `qemu-img convert -c`.

## Why binfmt is required

Do not "simplify" this to only `chroot ... qemu-aarch64-static /bin/bash` on an Arch x86_64 host. That can start an ARM shell but child ARM commands can fail with `Exec format error`. The project intentionally uses `binfmt_misc` so nested ARM executables run transparently.

On the CachyOS host, `qemu-user-static-binfmt` supplies the AArch64 registration. The builder restarts `systemd-binfmt.service` and checks for:

```text
/proc/sys/fs/binfmt_misc/qemu-aarch64
```

### binfmt / Exec format error diagnostics

Collect:

```bash
pacman -Q qemu-user-static qemu-user-static-binfmt
systemctl status systemd-binfmt.service --no-pager
ls -la /proc/sys/fs/binfmt_misc/
cat /proc/sys/fs/binfmt_misc/qemu-aarch64
```

Then retry:

```bash
sudo systemctl restart systemd-binfmt.service
```

## Password handling

Passwords must remain interactive. Do not replace `passwd` with `chpasswd`, command-line passwords, environment variables, generated plaintext files, or secrets stored in the repo.

If an automated/noninteractive build is ever desired, treat that as a separate design decision.

## Default rootfs trust path

The default rootfs URL is currently:

```text
https://ca.us.mirror.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz
```

The expected Arch Linux ARM build-system signing fingerprint is:

```text
68B3537F39A313B3E574D06777193F152BDBE6A6
```

The fingerprint is pinned in `build.py`; do not accept an arbitrary key returned by the keyserver.

## Guest networking

`overlay/etc/systemd/network/20-oci.network` matches `en* eth*` and enables DHCP plus IPv6 RA. Do not pin a MAC address; OCI custom images need to adapt to the created VNIC.

During the build, `/etc/resolv.conf` is temporarily copied from the host. Before completion it is replaced with:

```text
/run/systemd/resolve/stub-resolv.conf
```

and `systemd-resolved` is enabled.

## Boot path

OCI ARM uses UEFI. GRUB is installed with:

```text
--target=arm64-efi
--removable
--no-nvram
```

The critical output is:

```text
/boot/efi/EFI/BOOT/BOOTAA64.EFI
```

`templates/grub.cfg` boots Arch Linux ARM's `/boot/Image` and sends console output to both `tty0` and OCI ARM's `ttyAMA0` at 115200 baud.

If the OCI VM does not boot, collect the OCI serial-console output before changing bootloader code.

## Root filesystem expansion

The imported source image is intentionally small (10G by default). `overlay/usr/local/sbin/oci-grow-root` discovers the root partition dynamically, runs `growpart`, then `resize2fs`. `overlay/etc/systemd/system/oci-grow-root.service` runs once, guarded by `/var/lib/oci-root-grown`.

If expansion fails after boot, collect:

```bash
lsblk -f
findmnt /
systemctl status oci-grow-root.service --no-pager
journalctl -u oci-grow-root.service -b --no-pager
```

## SSH/firewall diagnostics after first boot

From serial console:

```bash
ip addr
ip route
networkctl status
systemctl status systemd-networkd systemd-resolved nftables sshguard sshd --no-pager
ss -lntup
nft list ruleset
sshd -T | grep -E '^(allowusers|passwordauthentication|pubkeyauthentication|permitrootlogin|maxauthtries)'
getent passwd alarm nullstring root
awk -F: '($3==0 || ($3>=1000 && $3<65534)) && $7 !~ /(nologin|false)$/ {print $1, $3, $7}' /etc/passwd
```

Expected SSH-effective policy includes:

```text
allowusers nullstring
passwordauthentication yes
pubkeyauthentication no
permitrootlogin no
```

## OCI-side settings

Expected custom image / instance settings:

```text
Image type:   QCOW2
OS:           Linux
Launch mode:  Paravirtualized
Firmware:     UEFI_64
Shape:        VM.Standard.A1.Flex
```

OCI VCN security lists / NSGs are a separate outer firewall. When troubleshooting unreachable SSH, check both OCI ingress rules and guest nftables before changing sshd.

## Host dependency installer

`install-deps.sh` supports pacman and apt. Its intent is specifically to avoid needless reinstalls: it checks the package database first and passes only missing package names to the package manager.

`requirements.txt` intentionally has no PyPI packages because `build.py` is standard-library-only.

## Useful builder debugging

Run:

```bash
sudo ./build.py --keep-work
```

The builder prints every external command before running it. On failure, preserve the `/var/tmp/oci-archarm.*` directory and report:

```bash
find /var/tmp/oci-archarm.* -maxdepth 3 -type f -o -type l | sort
losetup -a
mount | grep oci-archarm
```

Be careful with cleanup while the image is mounted. Prefer fixing `Builder.cleanup()` rather than issuing broad `umount`/`losetup` commands that could affect unrelated devices.

## Areas most likely to need compatibility fixes

- Arch Linux ARM package changes (`grub`, `linux-aarch64`, mkinitcpio behavior).
- QEMU/binfmt package naming on future Debian/Ubuntu hosts.
- OCI firmware/custom-image capability changes.
- NIC naming that falls outside `en* eth*`.
- Root device/partition naming assumptions in `oci-grow-root`.
- SSHGuard nftables backend path or service ordering changes.

When changing any of these, preserve the security invariants above and keep new static configuration out of inline Python strings where practical.
