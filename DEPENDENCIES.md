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

Supermin needs read access to a kernel image to construct the libguestfs
appliance. GitHub's hosted ARM runner restricts its Azure boot kernel, so the
release workflow's `ci/prepare-libguestfs.sh` helper uses `sudo` once per job to
make a mode-0644 copy in the ephemeral runner temp directory. It then uses
supermin's documented kernel-selection environment variables. Libguestfs and
the image builder themselves run unprivileged.

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

## Optional OCI deployment dependencies

`deploy-oci.py` uses Python 3.8 or newer plus `curl`, `ssh`, `ssh-keygen`, and
Oracle's `oci` CLI. It does not require the OCI Python SDK directly. A userland
installation with `pipx` is sufficient:

```bash
pipx install oci-cli
```

New OCI users should start with
[docs/OCI-PREPARATION.md](docs/OCI-PREPARATION.md), then continue with
[docs/OCI-DEPLOYMENT.md](docs/OCI-DEPLOYMENT.md).
