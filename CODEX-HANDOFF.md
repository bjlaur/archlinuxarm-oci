# Codex handoff: repository review and documentation refresh

Review date: 2026-08-23
Reviewed baseline: `0735370` (`main`)

## What changed

This worktree contains the full repository review and the implementation work
that followed it. The root [README](README.md) is now the short happy path for
downloading, verifying, and deploying an image. Detailed operating information
moved to [docs/README.md](docs/README.md), development and release instructions
to [DEVELOPERS.md](DEVELOPERS.md), and review findings to
[docs/CODE-REVIEW.md](docs/CODE-REVIEW.md). The focused OCI preparation,
deployment, and deployment-plan documents remain available and are linked from
the new navigation.

No operational guidance from the prior README or this handoff was intentionally
discarded. This file is the maintainer-oriented summary; the code review holds
the detailed rationale and remaining findings.

### Code and safety fixes

- `deploy-oci.py --clean` deletes only resources explicitly recorded as
  created or uploaded by this deployment. Reused instances, images, objects,
  and buckets are retained.
- Cleanup restores the recorded OCI profile, config path, and region when the
  caller does not override them.
- Before terminating a live instance or deleting a live custom image, cleanup
  fetches it from OCI and requires its deployment UUID and release SHA-256 tags
  to match state. A mismatch fails closed before the destructive command.
- Malformed release checksums now follow the normal `DeploymentError` path
  instead of escaping as an `AttributeError`; uppercase hexadecimal remains
  accepted and normalized.
- Local build overwrite preflight now covers exactly the QCOW2, its checksum,
  and `build-info.json`. Interactive builds list collisions and prompt once;
  non-interactive builds require `--force`. A metadata file naming another
  image gets an explicit warning. The builder never clears the directory.
- Checksum and build-metadata files are replaced atomically through temporary
  files.

### CI and release hardening

- `.github/workflows/ci.yml` runs the complete offline unit suite, the excluded
  downloader tests, Python compilation, and shell syntax checks on pushes,
  pull requests, and manual dispatches. It intentionally does not build an
  image.
- The release job creates GitHub artifact attestations for the exact tested
  QCOW2, `.sha256`, and `build-info.json` before publishing. Users can verify a
  downloaded image with:

  ```bash
  gh attestation verify *.qcow2 --repo bjlaur/archlinuxarm-oci
  ```

- Every GitHub-maintained Action is pinned to a full immutable commit SHA. The
  readable major version remains in an inline comment.
- `.github/dependabot.yml` checks GitHub Actions weekly and groups pin updates
  for review.

## Intentional package policy

The build intentionally runs `pacman -Sy --needed`, not `pacman -Syu`. A full
upgrade makes construction take substantially longer and inflates the image.
This is technically a partial upgrade, but it is an accepted, narrow
factory-image state: the source is the latest signed upstream rootfs, only the
required packages are installed, and the finished filesystem and exact QCOW2
are validated and UEFI-smoke-tested.

Users are expected to run this soon after first login and before installing
additional packages:

```bash
sudo pacman -Syu
```

The happy-path README carries that instruction. The test that requires
`pacman -Sy` and rejects build-time `pacman -Syu` expresses current policy and
should not be removed as a casual cleanup.

## Standard validation

Run before committing:

```bash
python3 -m unittest discover -s tests -v
python3 tests/excluded_test_download_latest.py -v
python3 -m py_compile build.py deploy-oci.py download-latest.py
bash -n ci/*.sh guest/*.sh install-deps.sh
git diff --check
```

The optional OCI CLI command-surface check performs no API mutations, but it
requires the OCI CLI to be installed:

```bash
python3 tests/excluded_test_oci_cli.py -v
```

An end-to-end ARM build, exact-QCOW2 UEFI smoke test, and real OCI deployment
remain the acceptance tests for guest behavior and OCI integration.

Last local result for this handoff: 143 default tests and 5 downloader tests
passed; Python compilation, shell syntax, workflow/Dependabot YAML parsing,
local Markdown-link checks, and `git diff --check` also passed. The OCI CLI
command-surface test was not run because `oci` was not installed. A full image
build and real OCI deployment were outside this local validation pass.

## Deployment reminder

Prepare the tenancy with [docs/OCI-PREPARATION.md](docs/OCI-PREPARATION.md),
then run the read-only OCI validation first:

```bash
./deploy-oci.py --assign-public-ip --dry-run
```

Deploy with the verified download:

```bash
./deploy-oci.py \
  --assign-public-ip \
  --reuse-download \
  --verify-ssh \
  --cleanup-bucket
```

On interruption or a recoverable OCI error, keep `.deploy-oci-state.json` and
rerun the same command with `--resume`. To intentionally discard a recorded
deployment, use `./deploy-oci.py --clean`. Do not hand-edit ownership flags,
OCIDs, the deployment UUID, or release checksum in state.

## Remaining review items

The actionable remainder is intentionally small:

1. extend live ownership validation to Object Storage cleanup;
2. reject invalid builder resource and timeout values during argument parsing;
3. use a digest rather than size and mtime for staged raw-image identity; and
4. record a sanitized, release-specific real-OCI acceptance result.

Lower-priority items, including console-password prominence and the scope of
`BaseException` cleanup catches, remain in [docs/CODE-REVIEW.md](docs/CODE-REVIEW.md).

Never commit OCI API private keys, OpenSSH private keys, deployment state,
downloaded images, raw disks, signatures, serial logs with secrets, or
known-hosts state.
