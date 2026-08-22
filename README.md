# Arch Linux ARM for Oracle Cloud Infrastructure

This project publishes a community-built Arch Linux ARM AArch64 factory image
for Oracle Cloud Infrastructure Ampere A1 (`VM.Standard.A1.Flex`). The primary
deliverable is the boot-tested QCOW2 image on the
[latest GitHub Release](https://github.com/bjlaur/archlinuxarm-oci/releases/latest),
not the build scripts in this repository.

The image is based on the signed official Arch Linux ARM generic AArch64
rootfs. It is not an official Arch Linux or Arch Linux ARM image.

## Download and verify

Download the `.qcow2`, matching `.sha256`, and `build-info.json` assets from the
[latest release](https://github.com/bjlaur/archlinuxarm-oci/releases/latest).
With the GitHub CLI:

```bash
gh release download --repo bjlaur/archlinuxarm-oci \
  --pattern '*.qcow2' \
  --pattern '*.qcow2.sha256' \
  --pattern 'build-info.json'
sha256sum -c -- *.qcow2.sha256
```

`build-info.json` records the project commit, upstream rootfs URL and checksum,
pinned signing fingerprint, image checksum, and build/smoke acceleration modes.

## Import and launch on OCI

Upload the QCOW2 to OCI Object Storage, then import it with:

```text
Image type:   QCOW2
OS:           Linux
Launch mode:  Paravirtualized
Firmware:     UEFI_64
Shape:        VM.Standard.A1.Flex
```

Supply your SSH public key through OCI's normal instance-launch flow. After the
instance boots, connect as:

```bash
ssh alarm@INSTANCE_IP
```

The source disk is 4 GiB. `oci-grow-root.service` expands partition 2 and its
ext4 filesystem when the instance uses a larger OCI boot volume.

## Factory image security

| Account | Console password | SSH | sudo |
| --- | --- | --- | --- |
| `root` | locked | disabled | n/a |
| `alarm` | locked | OCI-provided public key only | passwordless |

Published factory images contain:

- no usable user or root password;
- no baked-in authorized SSH key;
- no persistent SSH host keys;
- no machine identity or random seed; and
- no cloud-init instance state from the build.

On first boot, cloud-init's Oracle datasource obtains the instance metadata,
applies OCI's SSH key to the existing upstream `alarm` account, and processes
supported user-data and hostname metadata. SSH password authentication and root
SSH remain disabled. nftables uses a default-deny inbound policy, and SSHGuard
is enabled.

## Automated releases

A lightweight scheduled job checks the small checksum file adjacent to the
upstream rootfs. A complete factory image is built when that rootfs changes,
when image-affecting code changes on `main`, or when a rebuild is manually
forced.

Every published image must pass:

1. detached OpenPGP verification of the rootfs against the pinned full
   fingerprint;
2. a rootless AArch64 configuration boot;
3. offline account, SSH, cloud-init, identity, and bootloader validation;
4. zstd QCOW2 conversion followed by `qemu-img check` and SHA-256 generation;
   and
5. a real AArch64 UEFI boot of that exact QCOW2 artifact, through a disposable
   overlay and with NoCloud metadata proving SSH-key provisioning and
   passwordless administration.

The converted QCOW2 and `build-state.json` are uploaded before the smoke job.
If smoke testing fails, that exact artifact remains available for reproduction;
failed builds do not replace the latest release.

## Development images

The builder also retains a development mode for manual testing. Development
images ask for a custom administrator and separate root/administrator
passwords. They enable password SSH only for that administrator and require its
password for sudo. Development builds also convert before smoke testing, so the
exact failed QCOW2 is retained for local diagnosis. Development images are
never published as releases.

## Building from source

Most users do not need this section. Building requires QEMU, libguestfs,
AArch64 UEFI firmware, GnuPG, curl, and Python 3.11 or newer.

```bash
./install-deps.sh
./build.py --check
```

Dependency installation may use `sudo` to install missing host packages. The
image builder itself is rootless, never invokes `sudo`, and refuses to run as
root.

Build a credential-free factory image:

```bash
./build.py --factory-image
```

Build a development image:

```bash
./build.py
./build.py --username myadmin
```

For disposable automated testing only, one command-line password can be used
for both development accounts. It may be visible in shell history and process
listings and must never be used for a real image:

```bash
./build.py --username testadmin --password TEST-ONLY-PASSWORD
```

### Staged and resumable builds

An explicit workspace is retained and must be empty for the build stage. Later
stages infer factory or development mode and the converted image name from its
versioned `build-state.json`. Both modes convert before boot-testing the exact
QCOW2:

```bash
./build.py --factory-image --work-dir /path/to/work --build-only
./build.py --work-dir /path/to/work --convert-only
./build.py --work-dir /path/to/work --smoke-test-only
```

When `--output` is omitted for a staged build, the QCOW2 is written inside the
workspace. Conversion records its filename, size, format, and SHA-256 so a
relocated workspace or downloaded CI artifact can be verified before smoke
testing.

Use a disk-backed workspace when `/tmp` is a RAM-backed tmpfs.

### QEMU acceleration

```text
--accel auto   default; use KVM on native ARM64 when it is actually usable,
               otherwise fall back to TCG
--accel kvm    require KVM and fail if it is unavailable
--accel tcg    always use portable software emulation
```

KVM can substantially accelerate package installation and the UEFI smoke boot.
TCG works on x86_64 hosts and ARM64 systems without accessible KVM. The same
selection is applied to both project-managed QEMU stages; libguestfs manages
its own appliance acceleration.

### Local checks

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile build.py
bash -n guest/*.sh install-deps.sh
./build.py --check
```

Build progress is colorized on terminals. Redirected output is plain text; set
`NO_COLOR=1` to disable color explicitly.
