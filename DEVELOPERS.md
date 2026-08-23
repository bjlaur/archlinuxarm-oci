# Developer guide

This document covers local image builds, repository structure, tests, CI, and
release maintenance. End users should start with [README.md](README.md).

## Repository map

| Path | Responsibility |
| --- | --- |
| `build.py` | Rootless build, conversion, inspection, and UEFI smoke orchestration |
| `download-latest.py` | Atomic release download and SHA-256 consistency checks |
| `deploy-oci.py` | Experimental OCI CLI deployment, resume, and cleanup |
| `guest/` | Scripts run inside build and smoke-test guests |
| `overlay/` | Files copied into the completed image |
| `templates/` | Rendered fstab, GRUB, systemd, cloud-init, and SSH configuration |
| `ci/` | Hosted-runner libguestfs preparation |
| `tests/` | Offline unit and repository-policy tests |
| `.github/workflows/` | Upstream polling and image release pipeline |

## Host requirements

The builder requires Python 3.11 or newer, QEMU for AArch64, `qemu-img`,
libguestfs/guestfish, AArch64 UEFI firmware, GnuPG, and `curl`.

Install only missing host packages:

```bash
./install-deps.sh
./build.py --check
```

The dependency installer may use `sudo` for the host package manager. The
builder itself is rootless, never invokes `sudo`, and refuses to run as root.
It does not use host mounts, loop devices, chroot, or `binfmt_misc`.

See [DEPENDENCIES.md](DEPENDENCIES.md) for package names, required commands,
firmware search paths, and the hosted-runner libguestfs exception.

## Build architecture

A complete factory build has these trust and validation stages:

1. Download the Arch Linux ARM generic AArch64 rootfs and detached signature.
2. Import the pinned full signing fingerprint into an isolated GnuPG home and
   verify the rootfs signature.
3. Extract the verified kernel and initramfs for a direct AArch64 QEMU boot.
4. Use one rootless guestfish session to create GPT partitions, filesystems,
   import the rootfs, and record filesystem UUIDs.
5. Install a build payload and boot the raw disk in QEMU.
6. Install packages and final configuration inside the AArch64 guest.
7. Shut down, remove build markers and factory identity state offline, then
   inspect accounts, SSH policy, cloud-init components, and first-boot state in
   a read-only guestfish session.
8. Convert the verified raw disk to zstd-compressed QCOW2 and run
   `qemu-img check`.
9. Boot that exact QCOW2 through an overlay and AArch64 UEFI. Factory smoke
   testing supplies NoCloud metadata to prove key provisioning, cloud-init,
   passwordless sudo, and SSH policy without modifying the source QCOW2.

The build and smoke QEMU stages emit explicit success and failure sentinels to
the serial log. Failed workspaces and CI artifacts retain the relevant image,
state, and serial logs for reproduction.

### Intentional package-database sync

`guest/configure.sh` intentionally uses `pacman -Sy --needed` to install the
small, explicit package set required by the image. It does not run
`pacman -Syu` during construction. On Arch-family systems this is technically a
partial upgrade: the package databases are current while packages inherited
from the signed upstream rootfs may be older.

This is an accepted image-build tradeoff, not an accidental omission. A full
upgrade adds substantial build time and significantly increases the published
image size. The input is the latest signed Arch Linux ARM rootfs, the build
installs only the required packages, and the completed filesystem and exact
QCOW2 are inspected and UEFI-smoke-tested. Within that narrow factory-image
workflow, the partial state is intentional and has no known compatibility
issue.

The image is not intended to remain in that state indefinitely. End users are
told in the root README to run `sudo pacman -Syu` soon after first boot and
before installing more packages. Keep the test that asserts `pacman -Sy`
without `pacman -Syu`; changing this policy requires measuring build time and
image size and completing an end-to-end ARM build and UEFI smoke test.

## Build commands

Build the credential-free factory image:

```bash
./build.py --factory-image
```

Build a development image with prompted root and administrator passwords:

```bash
./build.py
./build.py --username myadmin
```

Development images remove the upstream `alarm` account, enable password SSH
only for the chosen administrator, disable public-key SSH, and require the
administrator password for sudo. They are never published.

For disposable automated testing only, `--password` uses one value for both
development accounts. It can be exposed in shell history and the process list:

```bash
./build.py --username testadmin --password TEST-ONLY-PASSWORD
```

Never use that option for a real image.

### Staged and resumable builds

Use an empty, disk-backed work directory when `/tmp` is a RAM-backed tmpfs:

```bash
./build.py --factory-image --work-dir /path/to/work --build-only
./build.py --work-dir /path/to/work --convert-only
./build.py --work-dir /path/to/work --smoke-test-only
```

The build stage records versioned `build-state.json`. Later stages infer the
factory/development mode, image user, root UUID, converted filename, size,
format, SHA-256, and acceleration choices. When `--output` is omitted, the
converted image is written inside the workspace.

An explicit work directory is retained. An implicit temporary directory is
removed after success unless `--keep-work` is used. Cleanup is deliberately
restricted to builder-named directories directly below the system temporary
directory.

### Acceleration

```text
--accel auto   Use KVM on native ARM64 when a live probe succeeds; otherwise TCG.
--accel kvm    Require KVM and fail when it is unavailable or inaccessible.
--accel tcg    Always use portable software emulation.
```

The selection applies to both project-managed QEMU stages. TCG uses a
Neoverse-N1 CPU model to avoid QEMU FEAT_MOPS faults. Libguestfs uses its direct
backend with workspace-local cache, temporary, and runtime directories;
explicit TCG also sets `force_tcg` for its appliance.

AArch64 UEFI firmware is autodetected from either pair:

```text
/usr/share/AAVMF/AAVMF_CODE.fd + AAVMF_VARS.fd
/usr/share/edk2/aarch64/QEMU_EFI.fd + QEMU_VARS.fd
```

Use `--firmware-code` and `--firmware-vars` together for custom paths.

## Local validation

Run all default offline tests and syntax checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile build.py deploy-oci.py download-latest.py
bash -n ci/*.sh guest/*.sh install-deps.sh
./build.py --check
```

Downloader tests are deliberately outside default unittest discovery because
of their filename:

```bash
python3 tests/excluded_test_download_latest.py -v
```

The OCI CLI command-surface test reads local `oci --help` output and performs
no API calls, but it requires the CLI to be installed:

```bash
python3 tests/excluded_test_oci_cli.py -v
```

An end-to-end image build and UEFI boot remains the definitive test for changes
to packages, boot files, systemd units, cloud-init, or guest configuration.

## CI and releases

`ci.yml` runs the complete offline unit suite, downloader tests, Python
compilation, and shell syntax checks for pushes, pull requests, and manual
dispatches. It deliberately does not install QEMU/libguestfs or build an image.

`check-upstream.yml` runs daily, after pushes to `main`, and on manual
dispatch. It downloads only the small MD5 file adjacent to the upstream rootfs
and compares it with the latest release metadata. MD5 is used only as a change
detector; the actual rootfs is authenticated by OpenPGP during the build.

When the upstream checksum changes, the checker dispatches `release.yml`. A
maintainer can also manually force a rebuild for project-code changes. A push
alone does not replace a release when the upstream rootfs is unchanged.

For a deliberate replacement, manually dispatch `release.yml` with
`replace_release=true`. Replacement implies a forced build. The workflow keeps
the current release available while the new image builds and passes its UEFI
smoke test, publishes the replacement, and only then deletes the previously
current release and its tag. Do not manually delete the current release first.

The release pipeline:

1. decides whether a build is needed;
2. runs source checks on an ARM runner;
3. builds and validates the raw factory image;
4. converts it to QCOW2 and uploads the exact artifact plus state;
5. downloads that artifact in a separate job and UEFI-smoke-tests it;
6. verifies the smoke result and image hash in the publish job; and
7. creates signed build-provenance attestations for the tested artifacts; and
8. publishes the QCOW2, `.sha256`, and `build-info.json` in one GitHub Release.

Branch validation must set `publish_release=false`. `smoke_source_run_id` can
retest a retained factory-image artifact from an earlier workflow run without
rebuilding it. Failed build and smoke artifacts are retained for seven days.

All third-party workflow references are pinned to full commit SHAs. Dependabot
checks those pins weekly and proposes grouped updates whose diffs can be
reviewed before the workflow code changes.

## Changing the image

When modifying a template, overlay, or guest script:

- update the repository-policy assertions in `tests/test_build.py`;
- extend completed-image inspection when the new property is security- or
  boot-critical;
- extend the UEFI smoke test when the property must survive a real boot; and
- force a non-publishing branch build before publishing a replacement image.

Keep factory identity removal offline, after systemd can no longer regenerate
it. Do not add private keys, passwords, OCI API data, machine IDs, random seeds,
SSH host keys, or cloud-init instance state to a factory payload.

When modifying `deploy-oci.py`, preserve these invariants:

- OCI commands use argument arrays, not shell command strings;
- mutations occur only after dry-run validation;
- state is private, atomic, and written at mutation checkpoints;
- recovery fails closed when tagged-resource discovery is ambiguous;
- cleanup acts only on resources whose ownership flags permit it; and
- private-key contents never enter commands, logs, or state.

Never commit downloaded images, raw disks, signatures, deployment state,
known-hosts state, OCI API private keys, or instance SSH private keys.

## Repository guidance and review

- [Repository instructions](AGENTS.md) records the durable safety and
  validation invariants for automated contributors.
- [Potential future work](docs/POTENTIAL-FUTURE-WORK.md) records deferred
  hardening and maintenance ideas.
