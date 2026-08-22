# archlinuxarm-oci

Build a hardened Arch Linux ARM AArch64 QCOW2 custom image for Oracle Cloud
Infrastructure Ampere A1 (`VM.Standard.A1.Flex`).

The image starts from the signed, official generic Arch Linux ARM root
filesystem. The host-side build is fully rootless: privileged configuration
runs inside a disposable emulated AArch64 VM, never on the host.

## Security model

The official generic rootfs includes the stock `alarm` account. The build VM is
started without inbound port forwarding and does not start the normal SSH
service. Before the finished image can boot normally, the builder:

- deletes `alarm` and all other ordinary human accounts;
- prompts for one administrative username;
- sets new root and administrator passwords interactively;
- permits SSH password login only for that administrator;
- disables root SSH, public-key auth, and keyboard-interactive auth;
- requires the administrator's password for sudo;
- installs a default-deny nftables firewall and SSHGuard; and
- deletes machine identity, random seed, and SSH host keys for first-boot regeneration.

| Account | Console | SSH | sudo |
| --- | --- | --- | --- |
| `root` | yes | **no** | n/a |
| prompted administrator | yes | password | password required |
| `alarm` | **deleted** | no | no |

System/service accounts remain and use noninteractive shells.

## Build

```bash
./install-deps.sh
./build.py --check
./build.py
```

There is no `sudo` prompt. The builder uses:

1. `guestfish` to create the GPT disk, EFI/ext4 filesystems, and import the
   verified rootfs without mounting it on the host;
2. `qemu-system-aarch64` to boot that disk and perform all privileged guest
   configuration; and
3. a disposable QCOW2 overlay for a real AArch64 UEFI boot smoke test.

The normal build prompts for the admin username, then separately prompts and
confirms the root and administrator passwords before starting any build work.
Passwords are retained only in process memory, automatically supplied to the
guest's `passwd` prompts, and never echoed or written into build files/logs.

Default output:

```text
archlinuxarm-oci.qcow2
```

Useful options:

```bash
./build.py --username myadmin --hostname oracle-arm
./build.py --image-size 10G --output archlinuxarm-oci.qcow2
./build.py --keep-work
```

### Staged and resumable builds

Use `--work-dir` to place every intermediate artifact in an exact directory.
An explicit workspace is always retained. New build workspaces must be empty;
the stage-only modes resume an existing workspace:

```bash
./build.py --work-dir /path/to/oci-work --build-only --username myadmin
./build.py --work-dir /path/to/oci-work --smoke-test-only
./build.py --work-dir /path/to/oci-work --convert-only --output archlinuxarm-oci.qcow2
```

`--build-only` creates and validates the raw disk. `--smoke-test-only` boots
that disk through UEFI using a disposable overlay. `--convert-only` performs
the final raw-to-compressed-QCOW2 conversion and refuses to run unless the
workspace records a successful smoke test for the unchanged raw disk. QCOW2
clusters are compressed with zstd.

This is also useful when `/tmp` is backed by RAM: choose a workspace on a
disk-backed filesystem instead.

Build progress is color-coded when standard output or standard error is a
terminal. Redirected output remains plain text. Set `NO_COLOR=1` to disable
colors explicitly.

### Automated test password

For disposable testing only, the same password can be supplied for root and
the administrator:

```bash
./build.py --username testadmin --password TEST-ONLY-PASSWORD
```

This is intentionally noisy and unsafe: the value can appear in shell history
and the host process list. It is sent only to the guest's interactive `passwd`
prompts and is not written into the image payload or serial log. Do not use
this option for the real OCI image.

## Verification performed automatically

The build fails unless all of these complete:

- detached rootfs signature verification against pinned fingerprint
  `68B3537F39A313B3E574D06777193F152BDBE6A6`;
- exact interactive-account check (`root` plus the prompted administrator);
- effective SSH, nftables, kernel, initramfs, and fallback EFI loader checks;
- clean AArch64 build-VM shutdown with an explicit success marker;
- actual boot through AArch64 UEFI from a disposable overlay; and
- `qemu-img check` on the compressed final QCOW2.

Run the local test suite with:

```bash
python -m unittest discover -s tests -v
```

## OCI import

Upload the QCOW2 to OCI Object Storage and import it as:

```text
Image type:   QCOW2
OS:           Linux
Launch mode:  Paravirtualized
Firmware:     UEFI_64
Shape:        VM.Standard.A1.Flex
```

The source disk defaults to 10 GB. `oci-grow-root.service` expands partition 2
and its ext4 filesystem when OCI launches it on a larger boot volume.

The kernel enables `ttyAMA0`; OCI's ARM serial console remains available as
break-glass access with the root password.

## Repository layout

```text
build.py                 rootless host orchestration
install-deps.sh          installs only missing host packages
guest/                   commands run inside the AArch64 build/smoke VMs
overlay/                 static files copied into the image
templates/               build-time UUID/username substitutions
tests/                   host-side unit and password-PTY tests
```
