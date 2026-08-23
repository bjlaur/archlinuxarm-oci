# Repository instructions

These instructions apply to the entire repository.

## Project boundaries

This project builds community-maintained Arch Linux ARM AArch64 images for
Oracle Cloud Infrastructure Ampere A1. Do not describe the images as official
Arch Linux, Arch Linux ARM, or Oracle images. Do not claim a release has passed
real OCI acceptance unless the repository contains a dated, sanitized record
for that exact release.

Keep the builder rootless. `build.py` must not invoke `sudo`, mount guest
filesystems on the host, use loop devices, chroot, or depend on `binfmt_misc`.
The signed upstream rootfs, pinned signing fingerprint, completed-filesystem
inspection, QCOW2 validation, and exact-QCOW2 AArch64 UEFI smoke test are trust
boundaries; do not weaken or bypass them to make a build pass.

`guest/configure.sh` intentionally runs `pacman -Sy --needed`, not
`pacman -Syu`. This accepted factory-image tradeoff keeps builds smaller and
faster. Do not change the policy or remove its test without measuring the build
impact and completing an end-to-end ARM build and UEFI smoke test. Users must
still be told to run `sudo pacman -Syu` soon after first boot.

Factory images must not retain private keys, passwords, authorized keys, SSH
host keys, machine IDs, random seeds, cloud-init instance state, or build
credentials. Development images are credentialed and must never be published.

## OCI deployment invariants

- Invoke the OCI CLI with argument arrays, never shell-built command strings.
- Keep dry-run free of OCI mutations.
- Keep deployment state private, atomic, and written at mutation checkpoints.
- Preserve graceful first-interrupt handling; a second interrupt is the
  explicit immediate-stop path.
- Fail closed when tagged-resource recovery is ambiguous.
- Cleanup may act only when recorded ownership flags allow it. Live compute
  resources must also match the deployment UUID and release SHA-256 tags.
- Reuse the profile, config path, and region recorded in state unless the user
  explicitly overrides them.
- Never place OCI config contents, API keys, SSH private-key contents, tokens,
  passwords, or pre-authenticated URLs in commands, logs, tests, or state.

Treat a deployment state file as authorization for destructive cleanup. Do not
hand-edit ownership flags, resource OCIDs, deployment UUIDs, or release hashes.
Object Storage live-marker validation remains an outstanding hardening item;
see `docs/POTENTIAL-FUTURE-WORK.md`.

## Build and release invariants

The QCOW2, its `.sha256` file, and `build-info.json` form one output group.
Overwrite checks must cover all three, and metadata writes must remain atomic.
The release pipeline must smoke-test the exact uploaded QCOW2 before publishing
it and attest the tested artifacts. GitHub Actions must remain pinned to full
commit SHAs; Dependabot manages pin updates.

Do not commit downloaded images, raw disks, signatures, deployment state,
known-hosts files, serial logs containing secrets, OCI credentials, or SSH
private keys. Preserve unrelated local runtime artifacts when editing a dirty
worktree.

## Change discipline

Update tests and documentation with behavior changes. Security-, boot-, or
first-boot-critical image changes should extend completed-image inspection and,
when appropriate, the UEFI smoke test. Keep source CI offline; a full image
build and real OCI deployment are explicit acceptance steps, not ordinary unit
tests.

Run the relevant checks before committing. The standard local suite is:

```bash
python3 -m unittest discover -s tests -v
python3 tests/excluded_test_download_latest.py -v
python3 -m py_compile build.py deploy-oci.py download-latest.py
bash -n ci/*.sh guest/*.sh install-deps.sh
git diff --check
```

When the OCI CLI is installed, also run the read-only command-surface test:

```bash
python3 tests/excluded_test_oci_cli.py -v
```

Use `README.md` for the user happy path, `docs/README.md` for technical
operation, `DEVELOPERS.md` for build and release maintenance, the focused OCI
guides for tenancy and deployment procedures, and
`docs/POTENTIAL-FUTURE-WORK.md` for non-committed future ideas.
