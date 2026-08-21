# Codex handoff: archlinuxarm-oci

## Goal and current state

This repository builds an Arch Linux ARM AArch64 QCOW2 custom image for OCI
Ampere A1 (`VM.Standard.A1.Flex`) from the signed official generic rootfs.

`build.py` is the production, rootless implementation. `legacy-build.py` is the
previous sudo/loop/chroot builder, retained only until the rootless path has
completed an end-to-end run on the user's CachyOS x86_64 host.

Host-side unit and syntax tests pass. The Work environment used for the rewrite
did not contain `qemu-system-aarch64`, `qemu-img`, or `guestfish`, so it could
not perform the full image/UEFI run. Do not describe the end-to-end acceptance
criteria as proven until a real build emits both success markers and produces a
QCOW2 passing `qemu-img check`.

## Non-negotiable security behavior

- Verify the rootfs signature against full pinned fingerprint
  `68B3537F39A313B3E574D06777193F152BDBE6A6`.
- Delete `alarm` and every other ordinary UID 1000-65533 login.
- Prompt for the admin username; never hardcode a personal username.
- Create exactly one normal admin in `wheel`; sudo requires its password.
- Set new root and admin passwords interactively on the QEMU serial terminal.
- Permit password SSH only for the selected admin.
- Disable root SSH, public-key auth, and keyboard-interactive auth.
- Preserve default-deny nftables plus SSHGuard's nftables backend.
- Delete machine identity, random seed, and host keys before the source image is
  converted so every OCI instance generates unique state.
- Keep OCI DHCP, `ttyAMA0`, UEFI fallback boot, and root-volume growth.

`--password` is an explicit user-requested exception for disposable automated
tests. It uses one value for both accounts and may expose that value in shell
history/process listings. The value must never be written to payloads, image
files, environment variables, or serial logs. The default remains interactive.

## Rootless architecture

1. Refuse EUID 0 and check `guestfish`, QEMU, GPG, curl, and AArch64 firmware.
2. Download the official rootfs and detached signature.
3. Verify the pinned signing key in an isolated temporary GnuPG home.
4. Extract `/boot/Image` only after signature verification.
5. Create a sparse raw disk with `qemu-img`.
6. Use rootless `guestfish` to create GPT, a 512 MiB FAT ESP, an ext4 root
   partition, and import the official tarball with numeric metadata/xattrs/ACLs.
7. Import a root-owned non-secret payload made from `overlay/`, `guest/`, and
   rendered `templates/`. Only fstab, temporary build networking/DNS, and the
   build service are installed at their live paths before boot; package-owned
   final configuration remains under the builder directory.
8. Direct-boot the verified kernel in `qemu-system-aarch64` with the raw disk as
   `/dev/vda`, serial I/O on a host PTY, QEMU user networking, and no inbound
   port forwarding.
9. `guest/build-entrypoint.sh` performs the full update, then applies the final
   configuration after package installation before doing account/password work,
   bootloader installation, service enablement, sanity checks, identity cleanup,
   and writing `/var/lib/archlinuxarm-oci/build-success`.
10. The host validates that durable marker, `/etc/passwd`, and the SSH drop-in,
    then removes the marker.
11. Create a disposable QCOW2 overlay, inject a smoke-only systemd service and
    GRUB command line, and boot through AArch64 UEFI without `-kernel`.
12. Require `OCI_IMAGE_UEFI_SMOKE_SUCCESS`, then convert the untouched source
    raw disk to compressed QCOW2 and run `qemu-img check`.

At no point may the host use sudo, loop devices, filesystem mounts, chroot,
binfmt, TAP, bridges, or privileged networking helpers.

## Important files

- `build.py`: rootless orchestration, PTY/password automation, markers, QEMU.
- `guest/build-entrypoint.sh`: privileged configuration inside build VM.
- `guest/configure.sh`: pacman update, package install, account creation.
- `guest/finalize.sh`: initramfs, removable ARM64 GRUB, service enablement.
- `guest/uefi-smoke-test.sh`: security/boot checks in disposable overlay.
- `overlay/`: final static firewall, SSHGuard, networking, sudo, grow-root.
- `templates/oci-image-build.service`: serial-attached build service.
- `templates/grub-smoke.cfg`: selects smoke service only in disposable overlay.
- `tests/test_build.py`: validation, root-owned tar, and non-logged password PTY test.

## Expected success output

The build VM serial log must contain:

```text
OCI_IMAGE_BUILD_SUCCESS
```

The separate UEFI boot log must contain:

```text
OCI_IMAGE_UEFI_SMOKE_SUCCESS
```

Neither marker is inferred from a clean QEMU exit; both are required explicitly.

## First real run on CachyOS

```bash
./install-deps.sh
./build.py --check
python -m unittest discover -s tests -v
./build.py --username testadmin --password TEST-ONLY-PASSWORD --keep-work
```

For the actual image, omit `--password` and enter distinct strong passwords.

When the first automated run fails, retain and inspect the printed
`/var/tmp/oci-archarm.*` workspace. Useful artifacts are:

```text
build-serial.log
uefi-smoke-serial.log
archlinuxarm-oci.raw
uefi-smoke.qcow2
payload.tar.gz
smoke-payload.tar.gz
```

Run `qemu-img check -f raw` only where appropriate; raw images do not carry
QCOW2 metadata. Use `guestfish --ro --format=raw -a ...` for offline inspection,
never mount the image on the host.

## Likely first-run compatibility points

- Arch package names: current expected host packages are
  `qemu-system-aarch64`, `qemu-img`, `edk2-aarch64`, and `libguestfs`.
- Generic ALARM kernel support for QEMU `virt`, virtio block/network/RNG, and
  observed root device `/dev/vda2`.
- Guestfish command/feature availability (`part-set-gpt-type`, vfat/ext4,
  `tar-in` xattrs and ACLs).
- `systemd.unit=oci-image-build.service` plus serial TTY password prompts.
- Network interface naming matched by `en* eth*` and QEMU user-network DHCP.
- Current Arch packages/paths for GRUB ARM64 and SSHGuard's nftables backend.
- Firmware paths. Auto-detected pairs are AAVMF CODE/VARS and Arch's
  `QEMU_EFI.fd`/`QEMU_VARS.fd`; CLI overrides exist.

Preserve `legacy-build.py` until the rootless build and actual UEFI smoke test
complete on the user's machine. After that proof, it can be deleted in a
separate, reviewable commit.

## OCI settings

```text
Image type:   QCOW2
OS:           Linux
Launch mode:  Paravirtualized
Firmware:     UEFI_64
Shape:        VM.Standard.A1.Flex
```

OCI network security lists/NSGs remain a separate outer firewall. Initially
restrict TCP 22 to the user's current public IPv4 `/32`; do not expose the
stock rootfs or build VM to inbound traffic.
