# ChatGPT handoff: rootless QEMU image builder

## Implementation status (Work mode pass)

The rootless implementation described below now exists in `build.py`, with the
old builder retained as `legacy-build.py`. Host-side syntax and unit tests pass,
including PTY automation that verifies the test password is not present in the
serial log. A full image build and UEFI boot still need to run on the user's
CachyOS host because the Work environment did not provide QEMU/libguestfs.

The user explicitly requested a test-only `--password` option after this
handoff was written. That narrow exception is implemented and documented; the
normal production path remains interactive.

## Objective

Replace the current host mount/chroot implementation with a fully rootless build
that runs all privileged guest operations inside a QEMU AArch64 virtual machine.

The user-facing build command must remain:

```bash
./build.py
```

The completed implementation must not invoke `sudo`, require host root, attach
loop devices, mount guest filesystems on the host, modify host `binfmt_misc`, or
restart host services.

Do not remove the existing builder until the rootless replacement has produced
and boot-tested an equivalent OCI image. Prefer introducing the new path in
small, reviewable stages.

## Repository context

This repository builds an Arch Linux ARM AArch64 QCOW2 image for Oracle Cloud
Infrastructure Ampere A1 (`VM.Standard.A1.Flex`) from the official generic
AArch64 root filesystem.

Read these files before changing anything:

```text
README.md
CODEX_HANDOFF.md
DEPENDENCIES.md
build.py
guest/configure.sh
guest/finalize.sh
guest/verify-login-users.sh
overlay/
templates/
```

`CODEX_HANDOFF.md` contains the existing design rationale, security policy,
boot details, OCI settings, and troubleshooting notes. Its security decisions
remain authoritative unless the user explicitly changes them.

## Non-negotiable security and access behavior

The rootless rewrite must preserve all of the following:

- Verify the downloaded rootfs signature against the pinned full fingerprint
  `68B3537F39A313B3E574D06777193F152BDBE6A6`.
- Delete the stock `alarm` account and all other ordinary UID 1000-65533 login
  accounts before the image can be used.
- Prompt for the administrative username; do not hardcode a personal username.
- Prompt interactively for new `root` and administrative-user passwords.
- Never pass production passwords through command-line arguments, environment
  variables, generated plaintext files, logs, or repository files. The
  user-requested `--password` command-line option is test-only and must warn
  that shell history/process listings can expose it; it must still never place
  the value in payloads, the image, environment variables, or serial logs.
- Create exactly one ordinary administrative login and add it to `wheel`.
- Require a password for sudo inside the guest.
- Disable root SSH access.
- Enable SSH password authentication only for the selected administrative user.
- Disable SSH public-key and keyboard-interactive authentication.
- Preserve the default-deny nftables firewall and SSHGuard nftables backend.
- Preserve fresh machine identity, random seed, and SSH host-key generation on
  first real boot.
- Preserve OCI DHCP networking, serial-console access, UEFI boot, and first-boot
  root-filesystem expansion.

Do not weaken these requirements merely to simplify automation.

## Why the existing builder uses sudo

The current `build.py` uses host loop devices, filesystem mounts, bind mounts,
`chroot`, ownership-preserving extraction, and system-wide AArch64 binfmt. Those
mechanisms require host privileges.

The replacement should avoid those mechanisms rather than trying to disguise
them behind rootless containers or user namespaces. In particular, do not rely
on unprivileged block-device mounts being available.

## Target architecture

Use a full `qemu-system-aarch64` virtual machine as the privilege boundary.
Processes may run as root inside the disposable build VM, but the QEMU process
and every host-side command must run as the invoking host user.

Recommended division of responsibility:

### Host-side Python

- Validate arguments and prompt for the administrative username.
- Download and verify the official rootfs and signature.
- Create the raw disk as a regular user-owned file.
- Partition and initially populate the disk using a rootless disk-image tool.
- Extract the AArch64 kernel needed for QEMU direct-kernel boot.
- Prepare a small, non-secret build payload containing guest scripts, overlay
  files, rendered non-secret configuration, and build metadata.
- Start `qemu-system-aarch64` with serial I/O connected to the terminal.
- Provide rootless outbound networking with QEMU user networking or `passt`.
- Wait for a deliberate success signal and clean VM shutdown.
- Boot-test the completed disk through AArch64 UEFI.
- Convert the raw disk to compressed QCOW2 and print its SHA-256 digest.

### Build VM

- Boot the AArch64 kernel with the new disk as its root device.
- Run the existing guest configuration logic as guest root.
- Perform `pacman -Syu` and install required guest packages.
- Delete unwanted login users and create the selected administrator.
- Run the interactive password prompts on the serial console.
- Apply the static overlay and render/install remaining configuration.
- Generate initramfs and install removable ARM64 UEFI GRUB.
- Validate users, sshd configuration, kernel, initramfs, and EFI loader.
- Remove build-only payloads and reset machine identity and SSH host keys.
- Write an explicit success marker to a small host-visible channel or disk and
  shut down cleanly.

## Suggested build flow

The exact tooling may change after a prototype, but the intended flow is:

1. Perform all argument and dependency checks before creating large files.
2. Download the official rootfs and detached signature as the host user.
3. Import only the pinned signing key into an isolated temporary GnuPG home and
   verify the signature.
4. Create a sparse raw disk and GPT layout without loop devices.
5. Create the 512 MiB FAT32 EFI partition and ext4 root partition using a
   rootless image-access mechanism such as libguestfs/guestfish.
6. Import the signed rootfs into the root partition while preserving numeric
   UID/GID ownership, permissions, symlinks, hard links, and device nodes.
7. Extract `/boot/Image` from the verified rootfs tarball for QEMU's `-kernel`
   option. Do not execute files taken from an unverified archive.
8. Place the build payload inside the guest filesystem or attach it as a second
   read-only disk. Do not put passwords in this payload.
9. Boot with `qemu-system-aarch64` using the `virt` machine, TCG acceleration,
   direct kernel boot, a virtio block device, serial console, and unprivileged
   user-mode networking.
10. Use a kernel command line appropriate to the QEMU `virt` machine, including
    the actual virtio root device and a serial console such as `ttyAMA0`.
11. Run the configuration/finalization scripts inside the VM and preserve the
    existing interactive password workflow.
12. Shut down only after all offline sanity checks succeed and a durable success
    marker has been written.
13. Start a second QEMU boot using AArch64 UEFI firmware and the disk's fallback
    loader, without `-kernel`, to confirm the final boot path actually works.
14. Make the UEFI smoke test non-destructive: do not complete first-boot identity
    generation or root growth against the source image unless the test uses a
    disposable QCOW2 overlay.
15. Convert the verified raw disk to the requested compressed QCOW2 output.

## Rootless disk manipulation

Prefer libguestfs/guestfish if it is available and reliable on the supported
Arch/CachyOS host. It can partition, format, mount within its appliance, import
tar archives, and edit regular disk-image files without host root.

Before committing to it, prototype and verify all of these operations:

- GPT creation with the required partition types and alignment.
- FAT32 and ext4 creation.
- Rootfs tar import with correct ownership, modes, symlinks, hard links, xattrs
  where applicable, and device nodes.
- File upload/download and atomic replacement.
- UUID discovery for fstab and GRUB rendering.
- Clean appliance shutdown with no lingering processes.

If libguestfs is unsuitable on Arch/CachyOS, a full QEMU bootstrap environment
may partition and populate the disk instead. Do not fall back to host loop
devices or mounts.

## QEMU considerations

- Use `qemu-system-aarch64`, not host binfmt, for the build VM.
- Use TCG because the host is x86_64 and the guest is AArch64.
- Prefer virtio block and network devices supported by the generic ALARM kernel.
- Use `-nographic` or an equivalent serial-only configuration so password
  prompts remain directly interactive.
- QEMU user networking must provide DNS and outbound HTTP/HTTPS for pacman.
- Do not require TAP devices, bridges, host firewall changes, or privileged
  networking helpers.
- Set deterministic timeouts for boot, build completion, and shutdown. On
  timeout, retain diagnostic artifacts when `--keep-work` is enabled.
- Capture a serial log, but ensure it cannot contain passwords. Normal `passwd`
  does not echo passwords; do not add terminal tracing around it.
- Use an ephemeral writable UEFI variables file if firmware requires one.
- Locate AArch64 UEFI firmware through a dependency/configuration check rather
  than assuming a single distribution-specific path.

## Build payload and guest entrypoint

Create a clear guest entrypoint rather than embedding a long shell program in
Python. Reuse and adapt `guest/configure.sh`, `guest/finalize.sh`, and
`guest/verify-login-users.sh`.

The entrypoint should:

1. Confirm it is running inside the expected AArch64 build VM.
2. Confirm the target root device and expected disk identity before modifying it.
3. Configure temporary networking and DNS if the generic rootfs does not do so.
4. Run configuration, password prompts, overlay installation, templating, and
   finalization in a logged sequence.
5. Treat every unexpected failure as fatal.
6. Never create the success marker after a partial failure.
7. Power off cleanly after success.

Avoid using a general-purpose SSH server as the host-to-build-VM control plane.
The serial console plus a narrow success channel is easier to audit and avoids
temporary credentials.

## UEFI boot validation

File-existence checks alone are insufficient for the rootless rewrite. After the
build VM installs GRUB, perform an actual UEFI boot using a disposable overlay.

The smoke test should confirm from serial output that:

- AArch64 UEFI finds `/EFI/BOOT/BOOTAA64.EFI`.
- GRUB loads the expected kernel and initramfs.
- Linux mounts the ext4 root filesystem.
- systemd reaches a defined target or emits a deliberate boot-success signal.
- No unexpected interactive UID accounts exist.

Do not mark the build successful merely because QEMU started. Detect a specific
guest-generated success condition and fail on panic, emergency mode, timeout,
or early QEMU exit.

## Dependency changes

Update `install-deps.sh`, `DEPENDENCIES.md`, and `README.md` together.

Expected new host dependencies may include:

- `qemu-system-aarch64` and `qemu-img`.
- AArch64 UEFI firmware suitable for QEMU `virt`.
- libguestfs/guestfish if chosen for image creation.
- Existing download and GnuPG tooling.

The final dependency installer must continue installing only missing packages.
Package names differ between Arch/CachyOS and Debian/Ubuntu, so detect supported
providers explicitly and produce actionable errors when unavailable.

Remove host dependencies that become unnecessary, including loop-device,
mount/chroot, and binfmt packages, only after the rootless path no longer uses
them.

## Compatibility and safety requirements

- Refuse to run as EUID 0 even though the new builder does not need root.
- Refuse or explicitly confirm overwriting an existing output image.
- Keep all temporary paths beneath a builder-created, validated directory.
- Never use broad recursive deletion on an unresolved path.
- Ensure cleanup can remove everything as the invoking user.
- Specify the input image format explicitly whenever calling QEMU/libguestfs.
- Do not open the same writable image concurrently from multiple processes.
- Use a disposable overlay for every boot test of the completed source image.
- Preserve the source raw image until conversion and verification complete.
- Keep static guest configuration in `overlay/`, guest commands in `guest/`, and
  build-time substitutions in `templates/`.

## Acceptance criteria

The rewrite is complete only when all of the following are demonstrated:

1. A normal user with the documented dependencies can run `./build.py` without
   any sudo prompt, privilege helper, host service change, loop device, or host
   filesystem mount.
2. The build succeeds on the user's x86_64 CachyOS/Arch host.
3. The final QCOW2 is owned by the invoking user and passes `qemu-img check`.
4. An actual AArch64 UEFI QEMU boot test reaches the defined success condition.
5. The image boots on OCI `VM.Standard.A1.Flex` using paravirtualized UEFI mode.
6. Only `root` and the prompted administrator are interactive human accounts;
   `alarm` is absent.
7. Effective sshd policy contains the selected `AllowUsers`, password auth is
   enabled, public-key auth is disabled, and root login is disabled.
8. nftables and SSHGuard start successfully and retain their intended ordering.
9. The root filesystem expands correctly when the OCI boot volume is larger than
   the source image, while real `growpart` failures remain retryable.
10. Machine identity and SSH host keys are unique on first real instance boot.
11. Passwords never appear in files, process arguments, environment variables,
    serial logs, or build logs.
12. Failure cleanup leaves no root-owned files and `--keep-work` retains useful
    unprivileged diagnostics.
13. Documentation accurately describes the rootless architecture and no longer
    instructs the user to install or configure host binfmt support.

## Implementation sequence

Use staged changes rather than one large rewrite:

1. Add a rootless dependency probe and a minimal QEMU AArch64 serial-boot proof
   of concept without changing the production builder.
2. Prototype rootless disk creation and signed-rootfs import; verify metadata.
3. Boot the populated rootfs by direct kernel boot and establish networking.
4. Move the existing guest configuration and interactive password flow into the
   build VM.
5. Install and validate GRUB, then add the disposable UEFI smoke test.
6. Integrate QCOW2 conversion, cleanup, diagnostics, and `--keep-work`.
7. Switch the default builder only after side-by-side validation succeeds.
8. Remove obsolete privileged code and dependencies in a final cleanup commit.

At every stage, keep the current security invariants testable and avoid mixing
unrelated refactors into the rootless migration.

## Questions to resolve during the prototype

- Which Arch/CachyOS package provides a working rootless libguestfs appliance?
- Which installed path supplies QEMU-compatible AArch64 UEFI firmware?
- Does the current generic ALARM kernel include the virtio block/network and
  QEMU `virt` platform drivers needed for direct boot?
- What root-device name is observed with the selected QEMU block controller?
- What is the smallest reliable success channel: serial sentinel, virtio-serial,
  fw_cfg, or a dedicated small status disk?
- Can the signed rootfs tar be imported directly with all required metadata, or
  is an in-VM extraction step necessary?
- How should the second UEFI smoke boot avoid consuming first-boot state while
  still proving systemd startup?

Answer these experimentally before locking the implementation to a particular
tool or firmware path.
