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

The normal build prompts for the admin username, root password, and admin
password. Password input is handled on the QEMU serial terminal and is not
echoed or written into build files/logs.

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
legacy-build.py          retained older sudo/loop implementation
install-deps.sh          installs only missing host packages
guest/                   commands run inside the AArch64 build/smoke VMs
overlay/                 static files copied into the image
templates/               build-time UUID/username substitutions
tests/                   host-side unit and password-PTY tests
CODEX_HANDOFF.md         design and troubleshooting handoff
```
