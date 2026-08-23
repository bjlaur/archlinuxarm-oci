# Potential future work

These are possible improvements, not commitments or authorization to make
changes. Re-evaluate each item against the current code, repository
instructions, operational experience, and project needs before implementing
it.

## Deployment cleanup hardening

### Verify Object Storage ownership markers

Compute cleanup validates deployment tags against live instances and custom
images. Object and bucket cleanup still relies on private state ownership
flags, object SHA-256 metadata, and the requirement that a bucket be empty. A
sufficiently corrupted state file could name an unrelated empty bucket.

A stronger design would persist and verify an OCI-side deployment marker for
created buckets and uploaded objects before deleting them. Cleanup should fail
closed when markers are missing, mismatched, or ambiguous.

## Builder validation and resumability

### Validate resource arguments at parse time

`build.py` parses `--memory`, `--cpus`, `--build-timeout`, and
`--smoke-timeout` as integers but does not require positive values. A shared
positive-integer argument validator would provide clearer errors and allow
direct boundary tests.

### Strengthen staged raw-image identity

Staged conversion checks the raw disk's size and nanosecond modification time.
That detects ordinary accidental changes but is not a content identity. A
future version could record and verify a raw-image SHA-256 at the
build-to-convert boundary, accepting the additional I/O cost.

## Real OCI acceptance testing

The repository does not yet contain a dated, sanitized acceptance record tying
an exact published image to a successful import and boot on OCI Ampere A1.
Occasional manual acceptance records could capture the release tag, region,
shape, image digest, import and capability results, boot result, SSH/cloud-init
checks, and root-growth result without tenancy identifiers or public IPs.

Automating that process is a larger, explicitly deferred proposal. See the
[OCI smoke CI plan](OCI-SMOKE-CI-PLAN.md) for its prerequisites, credential and
cleanup boundaries, proposed phases, and conditions for reconsidering it.

## Additional hardening ideas

### Revisit the console recovery password

The factory image deliberately retains the public upstream `alarm` password
for serial-console recovery while disabling SSH password authentication. The
documentation should keep that tradeoff prominent. A future mode could lock
the account after a verified launch-time SSH key is installed, provided the
recovery and first-boot implications are tested carefully.

### Narrow broad exception catches

Several atomic cleanup sections intentionally catch `BaseException` so cleanup
also runs for interrupts. Keep broad catches limited to narrowly scoped
cleanup, and prefer `Exception` plus explicit `KeyboardInterrupt` handling when
future control-flow changes make that practical.

## Completed work

Completed review findings are preserved in Git history and regression tests
rather than repeated here. Durable build, security, deployment, and release
invariants belong in [`AGENTS.md`](../AGENTS.md).
