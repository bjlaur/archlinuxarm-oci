# archlinuxarm-oci

Build a small, hardened Arch Linux ARM AArch64 QCOW2 custom image for Oracle Cloud Infrastructure Ampere A1 (`VM.Standard.A1.Flex`).

The project starts from the official Arch Linux ARM generic AArch64 root filesystem and builds the bootable disk locally. It does **not** use a third-party prebuilt VM image.

## Security model

The generic Arch Linux ARM rootfs includes the default `alarm` login account. This builder removes every ordinary human UID account before first boot, then prompts for and creates one administrative user.

Resulting interactive access:

| Account | Console | SSH | sudo |
| --- | --- | --- | --- |
| `root` | yes | **no** | n/a |
| prompted admin user | yes | password | yes, password required |
| `alarm` | **deleted** | no | no |

SSH public-key auth is intentionally disabled. SSH password auth is enabled only for the prompted administrative user. The image also enables a default-deny nftables firewall and SSHGuard.

System/service accounts remain in `/etc/passwd` as normal; they are not interactive login users.

## Build

```bash
./install-deps.sh
./build.py
```

`build.py` must be run as your normal user. It performs downloads, verification, image creation, templating, and QCOW2 conversion unprivileged. When it reaches operations that actually require root (loop devices, mounting, filesystem creation/extraction, chroot, and writes into the mounted guest), it invokes `sudo` for that individual command. The build starts with `sudo -v` so the host sudo prompt is separate from the later guest password prompts.

The builder deliberately refuses `sudo ./build.py`. This keeps the build workspace and final QCOW2 owned by your normal user and makes every privileged command visible in the log.

The build first prompts for the administrative username, then stops for normal `passwd` prompts for `root` and that administrative user. Passwords are never placed on a command line or stored in project files.

Default output:

```text
archlinuxarm-oci.qcow2
```

Useful options:

```bash
./build.py --image-size 10G --hostname oracle-arm --output archlinuxarm-oci.qcow2
# Optional: skip the username prompt
./build.py --username myadmin
./build.py --keep-work
```

`--keep-work` leaves the temporary `/var/tmp/oci-archarm.*` directory behind after a failure, which is useful for troubleshooting.

## OCI import

Upload the QCOW2 to OCI Object Storage, import it as a custom Linux image, and use:

```text
Image type:   QCOW2
Launch mode:  Paravirtualized
Firmware:     UEFI_64
Shape:        VM.Standard.A1.Flex
```

The source disk defaults to 10 GB. `oci-grow-root.service` expands partition 2 and its ext4 filesystem on first OCI boot, so launching it on a 50 GB (or larger) boot volume is expected.

The kernel command line enables `ttyAMA0`, and `serial-getty@ttyAMA0.service` is enabled for OCI ARM serial-console recovery.

## Repository layout

```text
build.py                 host-side image orchestration
install-deps.sh          pacman/apt host dependency installer
DEPENDENCIES.md          package/command details
requirements.txt         documents that Python has no PyPI dependencies
guest/                   scripts executed inside the AArch64 chroot
overlay/                 files copied verbatim into the finished image
templates/               files requiring build-time substitution (UUIDs/admin username)
CODEX_HANDOFF.md          troubleshooting/context handoff for coding agents
```

The intent is that security-sensitive configuration can be reviewed by opening the corresponding real file rather than searching through Python strings or shell heredocs.
