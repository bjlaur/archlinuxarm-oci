# Plan: real OCI deployment smoke testing from GitHub Actions

Plan date: 2026-08-23

Status: **proposal only; deferred unless the maintainer explicitly revives it**

Creating this plan does not authorize repository changes, GitHub Environment
changes, secret creation, OCI IAM changes, or OCI resource creation. A future
agent must stop after reviewing/updating the plan unless the maintainer
explicitly asks to proceed with implementation.

## Goal

Add an opt-in GitHub Actions acceptance test that deploys the exact
Arch Linux ARM QCOW2 to Oracle Cloud Infrastructure, verifies that it boots and
is usable, and then removes every resource created for the test.

This plan deliberately does not change the repository yet. Implementation
starts only after the current reviewed build and deployer have been smoke-tested
outside this new automation.

It may remain deferred indefinitely. A real-cloud CI system introduces
credential custody, cleanup risk, intermittent A1 capacity failures, and ongoing
maintenance. Those costs are probably unnecessary until the project has real
external users, regular releases, or enough deployment changes that manual OCI
acceptance testing becomes burdensome.

## Conditions for reviving the plan

Reconsider implementation when one or more of these becomes true:

- outside users begin downloading or deploying releases;
- releases become frequent enough that manual OCI testing is repetitive;
- OCI-specific regressions occur that offline and UEFI smoke tests did not
  catch;
- another maintainer needs a repeatable acceptance process; or
- release provenance needs to include demonstrated real-OCI boot evidence.

Until then, the existing offline tests, exact-QCOW2 UEFI smoke test, artifact
attestations, and occasional manual OCI deployment are a reasonable lower-cost
validation strategy.

## Recommended first version

The first version should be a manually dispatched workflow using a dedicated
GitHub Environment and a dedicated OCI compartment and automation identity.
It should not immediately become a required release gate. Once several manual
runs have proved that A1 capacity, networking, and cleanup are reliable, the
same tested script can be integrated into the release workflow.

Authentication should initially use a dedicated, narrowly scoped OCI API
signing key stored as a GitHub Environment secret. OCI's newer external-JWT
exchange can be evaluated later, but it adds enough identity-domain setup that
it should not block the first acceptance test.

## Intended end-to-end flow

1. A maintainer manually starts `oci-smoke.yml` against a release tag or an
   exact retained workflow artifact.
2. GitHub authorizes the `oci-smoke` Environment. Initially, this should require
   a maintainer approval and allow only the protected default branch.
3. The job installs a pinned OCI CLI version and creates an ephemeral OCI config
   and private-key file under `$RUNNER_TEMP`.
4. The job obtains and verifies the exact QCOW2 under test.
5. A dedicated network security group admits TCP/22 only from the current
   GitHub runner's public IPv4 `/32` address.
6. `deploy-oci.py` creates a uniquely named Object Storage bucket/object,
   imports a custom image, launches a minimal A1 instance, waits for boot, and
   verifies SSH/cloud-init behavior.
7. The workflow records the acceptance result, including release/artifact
   identity, region, shape, image SHA-256, and non-sensitive lifecycle results.
8. An unconditional cleanup phase terminates the instance and boot volume,
   deletes the custom image and temporary Object Storage resources, and removes
   the temporary NSG ingress rule.
9. A final read-only query verifies that no resource bearing the smoke-test run
   tags remains.
10. On failure, logs and the private deployment state are uploaded as a
    short-retention artifact for recovery. Credentials are never uploaded.

## Phase 1 — current manual smoke test

Owner: user/current operator

Before changing CI:

- start from a clean, current checkout of `main`;
- inspect the repository state with `git status` and record the exact commit;
- run the current documented dry-run;
- deploy the current published or locally built image using the current
  `deploy-oci.py` path;
- verify SSH, cloud-init completion, sudo, expected SSH policy, root filesystem
  growth, and normal boot;
- run `deploy-oci.py --clean` and confirm the instance, boot volume, custom
  image, object, and bucket are gone; and
- preserve only sanitized results: no tenancy identifiers, public IPs, private
  keys, state files, or usernames beyond the documented image user.

Exit criterion: the present deploy/resume/cleanup behavior has succeeded once
against real OCI, or any defects discovered during the test have been fixed and
retested.

## Phase 2 — refresh from the current repository

Owner: Codex, after the user confirms Phase 1

1. Clone or fetch the current `main` branch from
   `https://github.com/bjlaur/archlinuxarm-oci` into a clean workspace.
2. Record the exact baseline commit and inspect the current worktree, workflows,
   deployment script, tests, and documentation.
3. Review `AGENTS.md` and `docs/CODE-REVIEW.md`; do not blindly replay details
   from this deferred plan when the repository has changed.
4. Run all existing offline tests before editing.
5. Inspect the current release artifact boundary to determine whether the OCI
   test can consume the exact pre-release QCOW2 directly or initially needs to
   target a published release.
6. Update this plan inside the repository to reflect intervening changes and
   the real-OCI observations from Phase 1.

Exit criterion: a clean, tested baseline and an updated implementation diff
plan tied to its commit SHA.

## Phase 3 — repository changes

Owner: Codex

### 3.1 Strengthen cleanup ownership first

Before placing OCI credentials in automation:

- retain the current live deployment-tag checks for instances and images;
- add equivalent OCI-side ownership verification for created buckets and
  uploaded objects;
- record the deployment UUID and image SHA-256 in bucket tags and object
  metadata;
- require those markers before destructive `--clean` storage operations;
- fail closed on missing or mismatched live markers; and
- add tests for mismatched instance, image, bucket, and object ownership.

This closes the remaining state-file-only deletion path before CI relies on
automatic cleanup.

### 3.2 Add CI-oriented deployer inputs

Add generic features rather than GitHub-specific behavior where practical:

- `--nsg-id` to attach an existing dedicated NSG to the launched VNIC;
- repeatable additional freeform tags, while prohibiting callers from
  overriding the deployer's reserved ownership tags;
- stable machine-readable output or a small result JSON containing only
  non-secret deployment results;
- unique display/bucket names supplied by the workflow using the GitHub run ID;
  and
- an explicit verification mode that checks cloud-init completion, SSH key
  authentication, password-authentication rejection, passwordless sudo, and
  root filesystem growth.

Do not make the deployer manage general VCNs or security lists. The workflow
may manage one narrowly scoped rule on a pre-created smoke-test NSG.

### 3.3 Put orchestration in a testable script

Create `ci/oci-smoke.sh` (or an equivalently focused Python helper) so cleanup
and error handling are testable outside YAML. It should:

- use strict error handling without shell tracing;
- construct OCI configuration only under a private temporary directory;
- determine the runner's public IPv4 address;
- add a uniquely described `/32` TCP/22 ingress rule to the dedicated NSG;
- start the deployment with a private, explicit state-file path;
- preserve the primary test exit status;
- always attempt `deploy-oci.py --clean` when state exists;
- always remove only the exact NSG rule created for this run;
- verify that tagged resources are absent after cleanup;
- emit a sanitized result summary; and
- leave recoverable state only when cleanup fails.

The deployment command should use 1 OCPU, 6 GB RAM, a 50 GB boot volume, a
public IP, the dedicated subnet/NSG, unique names, `--verify-ssh`, and temporary
Object Storage cleanup. It should not run a full `pacman -Syu`; that tests Arch
mirrors and upgrade duration more than it tests the factory image.

### 3.4 Add the manual workflow

Create `.github/workflows/oci-smoke.yml` with:

- `workflow_dispatch` only at first;
- an input selecting a release tag or exact source workflow run;
- `environment: oci-smoke`;
- minimal `GITHUB_TOKEN` permissions (`contents: read`, plus `actions: read`
  only when downloading another run's artifact);
- no `pull_request` trigger and no use of secrets from untrusted code;
- immutable full-SHA Action references;
- `concurrency` limited to one OCI smoke deployment;
- a job timeout longer than the deployment timeout so cleanup still has time;
- an unconditional cleanup/result-upload step;
- short artifact retention for sanitized logs and failed cleanup state; and
- an explicit success condition requiring both workload checks and confirmed
  cleanup.

Start with published-release mode if the current workflow does not expose a
complete verified pre-release artifact set. Then extend the release build to
publish the QCOW2, checksum, and metadata together as an internal artifact so
the OCI job can test the exact bytes before public release.

### 3.5 Add leak detection

Hard termination of a GitHub runner can bypass in-process traps and final
steps. Add a separate manual/scheduled read-only audit that finds CI-tagged OCI
resources older than the expected job duration.

The first version should report stale resources and provide an exact recovery
command. It should not automatically delete resources without state. Automatic
janitor deletion can be considered only after every resource is consistently
tagged, the dedicated compartment contains no non-CI resources, and several
failure/recovery tests have succeeded.

### 3.6 Tests and documentation

Add offline tests for:

- reserved/additional tag merging;
- NSG command construction and validation;
- storage ownership mismatch refusal;
- cleanup after failures at each deployment checkpoint;
- preservation of the original failure when cleanup also fails;
- workflow triggers, environment binding, concurrency, permissions, timeouts,
  pinned Actions, and unconditional cleanup;
- absence of secret values and private-key contents from commands, state,
  result JSON, and logs; and
- exact-artifact identity through build, OCI test, attestation, and release.

Update the root README only with a short statement that releases may receive a
real-OCI acceptance check. Put setup and troubleshooting details in
`DEVELOPERS.md` or a focused `docs/OCI-SMOKE-CI.md`.

## Phase 4 — provision OCI and GitHub securely

Owner: Codex where authenticated tooling and permissions allow; otherwise
Codex supplies exact commands and walks the user through the blocked console
step.

### Preconditions Codex will check

- `oci` is installed and already authenticated as an identity permitted to
  manage the required IAM and test resources;
- `gh auth status` shows an identity with administration access to the GitHub
  repository;
- the intended tenancy, region, compartment, subnet, and NSG are unambiguous;
  and
- the user confirms the exact OCI targets before IAM or GitHub settings are
  changed.

### OCI resources

Prefer creating:

- a dedicated child compartment such as `archlinuxarm-oci-ci`;
- a dedicated IAM user such as `archlinuxarm-oci-github-actions`;
- a dedicated IAM group for that user;
- compartment-scoped policies sufficient only for custom images, A1 instances,
  their boot volumes, temporary Object Storage resources, and inspection/use of
  the selected subnet and NSG; and
- a dedicated NSG with no permanently broad SSH ingress rule.

If the subnet or NSG lives in a different network compartment, add only the
cross-compartment `use`/inspection permissions actually required. Do not grant
`manage all-resources` in the tenancy.

Codex can use the OCI CLI to create the user/group membership, API public key,
compartment/policy when authorized, and dedicated test network objects when
needed. It should first perform read-only discovery and show the resolved
targets. Permission errors are stop conditions, not reasons to broaden policy
silently.

### GitHub Environment configuration

The OCI CLI cannot set GitHub secrets. Codex can use the authenticated GitHub
CLI/API to create the `oci-smoke` Environment and set its values.

Sensitive secret:

- `OCI_PRIVATE_KEY_PEM`

Environment variables, or secrets if the user prefers to conceal identifiers:

- `OCI_TENANCY_OCID`
- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_REGION`
- `OCI_COMPARTMENT_OCID`
- `OCI_SUBNET_OCID`
- `OCI_AVAILABILITY_DOMAIN`
- `OCI_NSG_ID`

Codex should generate a signing key specifically for CI, upload only its public
key to the dedicated OCI user, write the private key directly into the GitHub
Environment secret, verify that the secret exists, and avoid printing or
placing the private key in repository files, handoff files, command arguments,
or artifacts. GitHub does not permit reading a secret value back; rotation
means creating a new OCI API key and replacing the GitHub secret.

Environment protection should initially require a maintainer reviewer and
restrict deployments to the protected default branch. After reliable manual
runs, the reviewer requirement can be reconsidered if fully automatic release
testing is desired.

## Phase 5 — handoff, push, and live test

Owner: Codex and user

### Durable implementation record

Update this plan and the focused operational documentation after implementation.
The durable record must contain:

- baseline and implementation commit SHAs;
- all files changed and why;
- exact local validation results;
- the GitHub Environment, variable, and secret names, but never their values;
- OCI resource names and sanitized purpose, but no private key or public IP;
- what Codex successfully provisioned through `oci` and `gh`;
- any console/user steps still required;
- the exact first manual workflow-dispatch procedure;
- cleanup and leaked-resource recovery procedures;
- API-key rotation/revocation instructions;
- known cost, capacity, timeout, and GitHub-runner networking failure modes;
- the criteria for enabling automatic release gating; and
- remaining work, especially OIDC migration and optional automatic janitor
  cleanup.

### Push sequence

1. Review the complete diff and confirm no credentials or OCI state are
   present.
2. Run all offline tests, syntax checks, workflow parsing, secret-pattern scans,
   and `git diff --check`.
3. Commit on a dedicated branch and push it.
4. Let ordinary source CI pass without OCI secrets.
5. Manually dispatch `oci-smoke.yml` against a known-good published release.
6. Approve the `oci-smoke` Environment when prompted.
7. Observe deployment, SSH validation, cleanup, and the final zero-resource
   check.
8. Confirm independently in OCI that no instance, boot volume, custom image,
   object, bucket, or temporary NSG rule remains.
9. Force one controlled failure after resource creation and prove that cleanup
   and recovery artifacts work.
10. Merge only after both the success path and controlled-failure cleanup pass.

### Later release integration

After several successful manual runs:

- allow the release workflow to invoke the tested OCI smoke helper against the
  exact pre-release artifact;
- decide whether OCI capacity/authentication failures block publishing or mark
  the release as lacking real-OCI acceptance;
- publish a sanitized acceptance record tied to the image SHA-256 and
  attestation; and
- evaluate replacing the long-lived API key with OCI external-JWT/OIDC trust.

## Failure and cleanup model

Cleanup is part of the test result, not best-effort decoration. A workload test
that passes but leaves resources behind is a failed run.

Normal script errors, expected timeouts, and ordinary job cancellation should
reach cleanup. Runner loss or forceful termination may not. That residual risk
is controlled through unique names, immutable ownership tags, a dedicated
compartment, short timeouts, retained state, a read-only stale-resource audit,
and a documented manual cleanup procedure.

## Cost and reliability boundaries

- A1 capacity can be unavailable even when the image is correct.
- The test temporarily consumes one A1 instance allocation, a 50 GB boot
  volume, Object Storage, and custom-image storage/quota.
- The workflow should run manually or per release, not on every push or pull
  request.
- Use OCI budgets/alerts and periodically audit the dedicated compartment.
- Do not treat a mirror-dependent full `pacman -Syu` as part of the initial
  image acceptance test.
- Do not claim cleanup is infallible; retain an independent recovery path.

## Decisions to revisit after the first live run

1. Whether a dedicated child compartment can be created or an existing test
   compartment must be used.
2. Whether the current subnet can safely support a dedicated NSG and public
   IPv4 SSH from GitHub-hosted runners.
3. Whether the first workflow tests a published release or a retained build
   artifact.
4. Whether OCI smoke failure should eventually block release publication.
5. Whether manual environment approval remains desirable after stabilization.
6. Whether to implement OCI external-JWT/OIDC exchange instead of an API key.
7. Whether a proven tag-based janitor may automatically remove stale CI
   resources.
