# Host dependencies

The production builder is fully rootless. It does not use host loop devices,
mounts, chroot, `binfmt_misc`, or `sudo`.

Run:

```bash
./install-deps.sh
```

The installer queries the package database and passes only missing package
names to pacman or apt.

## Arch / CachyOS

```bash
sudo pacman -S --needed python qemu-img qemu-system-aarch64 edk2-aarch64 libguestfs curl gnupg
```

## Debian / Ubuntu

```bash
sudo apt-get update
sudo apt-get install python3 qemu-utils qemu-system-arm qemu-efi-aarch64 libguestfs-tools curl gnupg
```

On native ARM64 Ubuntu hosts, `install-deps.sh` also installs
`linux-image-arm64`. Supermin needs an installed kernel image to construct the
libguestfs appliance; minimal and hosted ARM environments may not otherwise
provide one.

## Required commands

- `qemu-system-aarch64`: runs the disposable ARM build VM and UEFI smoke test.
- `qemu-img`: creates, overlays, validates, and converts disk images.
- `guestfish`: partitions, formats, and imports files without host mounts/root.
- `gpg`: verifies the official rootfs against the pinned signing fingerprint.
- `curl`: downloads the official rootfs and detached signature.
- Python 3.11 or newer: runs the standard-library-only orchestrator.

The UEFI test auto-detects either of these firmware pairs:

```text
/usr/share/AAVMF/AAVMF_CODE.fd + AAVMF_VARS.fd
/usr/share/edk2/aarch64/QEMU_EFI.fd + QEMU_VARS.fd
```

Custom locations can be supplied with `--firmware-code` and
`--firmware-vars`.

Check the environment without downloading or building anything:

```bash
./build.py --check
```
