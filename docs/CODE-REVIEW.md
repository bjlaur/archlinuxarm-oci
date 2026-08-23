# Repository code review

Review date: 2026-08-23
Reviewed baseline: `0735370` (`main`)

## Summary

The repository is unusually careful for a small image-building project. It
uses argument-array subprocess calls, rootless disk construction, isolated
OpenPGP verification, explicit lifecycle checks, atomic private deployment
state, fail-closed recovery, exact-artifact smoke testing, and a strong offline
test suite.

The review found three concrete deployment defects and several architectural
or maintenance risks. The concrete defects are fixed in the accompanying
change. The remaining findings are prioritized below rather than silently
changing intentional build or CI policy.

## Fixed in this review

### 1. `--clean` could delete reused Object Storage resources — high

`clean_object()` and `clean_bucket()` trusted the presence of a state record
but did not enforce its `uploaded` or `created` ownership flag. A deployment
that reused an existing object or bucket could later delete it with
`deploy-oci.py --clean`, contradicting the documented ownership model.

The cleanup path now:

- deletes an object only when `uploaded is True`;
- deletes a bucket only when `created is True`;
- deletes an image or instance only when `created is True`; and
- keeps pre-existing resources while still removing completed local state.

Regression coverage: `CleanDeploymentTests.test_clean_deployment_keeps_reused_object_and_bucket`.

### 2. `--clean` ignored the deployment's OCI context — high

Deployment state recorded the OCI profile, config path, and region, but
`clean_deployment()` constructed its OCI runner from fresh command defaults.
The documented `./deploy-oci.py --clean` command could therefore fail against
the wrong profile or region after a non-default deployment.

Cleanup now restores missing profile, config-file, and region values from the
state file while retaining explicit command-line overrides.

Regression coverage: `CleanDeploymentTests.test_clean_deployment_reuses_recorded_oci_context`.

### 3. Malformed release metadata could produce a traceback — medium

`parse_release()` called `.lower()` on `image_sha256` before confirming it was
a string. A malformed `build-info.json` value therefore escaped the intended
`DeploymentError` path as `AttributeError`.

The parser now validates the type and hexadecimal form first, then normalizes
case. A regression test covers a non-string checksum.

### 4. Release artifacts lacked signed provenance — hardening

The QCOW2 checksum and `build-info.json` were controlled by the same GitHub
Release. They detected corruption and inconsistency, but a party able to
replace every release asset could replace all three together.

The publish job now creates a signed [GitHub artifact
attestation](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
covering the tested QCOW2, checksum, and metadata before publishing the
release. Users can bind local bytes to this repository's workflow identity
with `gh attestation verify`.

### 5. Deployment and downloader tests lacked hosted CI — high

The repository previously had no ordinary push or pull-request source-check
workflow. The image release workflow intentionally ran only its image-focused
checks, so deployment and downloader regressions depended on local testing.

`ci.yml` now runs the complete offline unit suite, the separately named
downloader tests, Python compilation, and shell syntax checks on pushes and
pull requests without starting an expensive image build.

### 6. Destructive compute cleanup trusted only local state — high

Ownership flags prevented deletion of resources recorded as reused, but a
stale or accidentally edited state file could still point a created-resource
record at an unrelated live compute OCID.

Before a live instance is terminated or a live custom image is deleted,
`--clean` now requires the resource's deployment UUID and release SHA-256 tags
to match the state file. Missing or mismatched tags fail closed before the
destructive OCI command. Already absent or terminal resources remain safely
restartable.

Regression coverage:
`CleanDeploymentTests.test_clean_deployment_refuses_mismatched_live_compute_tags`.

### 7. Local builds could replace unrelated metadata — medium

The overwrite preflight checked only the QCOW2 even though conversion also
wrote its checksum and the directory-wide `build-info.json`. A differently
named build could therefore replace unrelated metadata without warning.

The builder now treats the QCOW2, its `.sha256` sidecar, and `build-info.json`
as one named output group. Interactive builds list all collisions and ask once;
non-interactive builds refuse unless `--force` is explicit. A metadata file
that names a different image receives an additional warning. Checksum and JSON
writes use temporary files followed by atomic replacement. No other directory
contents are removed.

### 8. Workflow action references were mutable — hardening

All GitHub-maintained Actions are now pinned to full 40-character commit SHAs,
with their release line retained in comments for readability. Dependabot checks
the pins weekly and proposes grouped updates for review.

## Accepted design decisions

### Intentional `pacman -Sy` during construction

`guest/configure.sh` deliberately runs `pacman -Sy --needed`, not
`pacman -Syu`. This is technically a partial upgrade, but a full upgrade makes
the build substantially slower and the resulting image significantly larger.
The input is the latest signed upstream rootfs, only the explicit required
packages are installed, and the completed filesystem and exact QCOW2 receive
structural validation and a UEFI smoke test.

The project accepts this narrow factory-image state with the expectation that
users run `sudo pacman -Syu` soon after first boot and before installing more
packages. The happy-path README makes that handoff explicit. The repository
test that prohibits a build-time `pacman -Syu` is intentional policy, not a
workaround awaiting removal.

## Outstanding findings

### Medium priority

#### Extend live cleanup ownership checks to Object Storage

Compute cleanup now validates deployment tags against live resources. Object
and bucket cleanup still relies on state ownership flags, object SHA-256
metadata, and the requirement that a bucket be empty. A sufficiently corrupted
state file could name an unrelated empty bucket.

Recommended change: persist and verify an OCI-side deployment marker for
created buckets and uploaded objects before deleting them. Continue treating
the private state file as sensitive in the meantime.

#### Validate builder resource arguments

`build.py` parses `--memory`, `--cpus`, `--build-timeout`, and
`--smoke-timeout` as integers but does not require positive values. Invalid
values fail later in QEMU or timeout behavior with less useful diagnostics.

Recommended change: share a positive-integer argparse validator and add
boundary tests.

#### Strengthen staged raw-image identity

Staged conversion checks the raw disk's size and nanosecond mtime rather than
its digest. This detects ordinary accidental changes but is not a content
identity. A modified raw image with restored timestamps can pass the state
check; completed-image inspection covers important properties but not every
file.

Recommended change: record and verify a raw-image SHA-256 at the build/convert
boundary, accepting the extra I/O cost for a stronger resumable-build claim.

#### Record real-OCI acceptance evidence

`docs/OCI-DEPLOYMENT-PLAN.md` correctly says the workflow is incomplete until
the exact published UEFI/GPT image boots on real A1 hardware, but the
repository contains no dated acceptance record tied to a release/tag.

Recommended change: add a compact acceptance record with release tag, region,
shape, import result, capability result, boot result, SSH/cloud-init check, and
root-growth result. Do not include tenancy identifiers or public IPs.

### Lower priority / hardening

#### Make the known console password more prominent

Retaining the upstream `alarm` password is a deliberate recovery choice and
SSH password authentication is disabled. Even so, the password is public and
serial-console IAM becomes security-sensitive.

Recommended change: keep the warning prominent, encourage a first-boot
password change, and consider a future mode that locks the account after a
verified key is installed.

#### Avoid broad `BaseException` cleanup catches where practical

Several atomic cleanup sections intentionally catch `BaseException` so they
also run for interrupts. This works, but it also catches `SystemExit` and other
process-control exceptions, making future control flow harder to reason about.

Recommended change: retain broad catches only around narrowly scoped resource
cleanup; use `Exception` plus explicit `KeyboardInterrupt` handling elsewhere.

## Test and documentation observations

- The default offline suite has good coverage of build policy, OCI command
  construction, lifecycle transitions, resume behavior, interruption, and
  destructive cleanup.
- The exact QCOW2 is moved through an artifact boundary before smoke testing,
  which prevents accidentally testing only the raw precursor.
- The default test discovery intentionally excludes downloader and OCI CLI
  command-surface tests. This is explained in `DEVELOPERS.md`, but hosted CI
  should still run the downloader suite.
- The old root README mixed download, deployment, security, release internals,
  local build instructions, and maintenance notes. Documentation is now split
  into a short happy path, this technical guide, focused OCI guides, and a
  developer guide without dropping the original operational information.

## Suggested order of follow-up work

1. Add OCI-side ownership validation for Object Storage cleanup.
2. Validate builder resource arguments at argument-parsing time.
3. Strengthen staged raw-image identity with a content digest.
4. Record a sanitized real-OCI acceptance result for a published tag.
