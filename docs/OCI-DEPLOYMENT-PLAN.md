# OCI deployment automation plan

## Goal

Add a supported, command-line deployment path from the latest published QCOW2
to a running Oracle Cloud Infrastructure Ampere A1 instance. The result should
be simple for a user who already has an OCI tenancy and network, while making
every cloud mutation visible, resumable, and narrowly scoped.

This work is not complete until the published UEFI/GPT image has been imported
and booted on real OCI `VM.Standard.A1.Flex` hardware. Oracle's general Linux
custom-image documentation still describes BIOS/MBR source images and does not
list Arch Linux ARM among its tested operating systems. The repository must
describe this as community-tested rather than Oracle-supported.

## Deliverables

1. `deploy-oci.py`: a Python 3.8+ command-line tool that orchestrates the OCI
   CLI without requiring the OCI Python SDK.
2. `docs/OCI-PREPARATION.md`: a one-time tenancy preparation guide.
3. `docs/OCI-DEPLOYMENT.md`: an end-to-end deployment and operations guide.
4. Offline unit tests for argument validation, OCI response parsing, state
   transitions, resume behavior, command construction, and cleanup decisions.
5. A short README section linking to both guides.

## Initial scope

The first version will:

- use the repository's `download-latest.py` to download and verify the current
  GitHub Release;
- authenticate through an existing OCI CLI profile;
- discover accessible subnets, derive the default deployment compartment from
  the selected subnet, and find A1-capable availability domains;
- generate or reuse a dedicated local Ed25519 key pair for instance access;
- use an existing private Object Storage bucket or create a private
  Standard-tier bucket after explicit option or interactive confirmation;
- upload the QCOW2 with OCI CLI multipart progress and checksum verification;
- import the object as a Linux QCOW2 with paravirtualized launch mode;
- wait for the custom image to become `AVAILABLE`;
- verify effective UEFI and paravirtualized image capabilities;
- add `VM.Standard.A1.Flex` to the compatible-shape list when absent;
- launch a configurable A1 Flex instance with a minimum 50 GB boot volume;
- wait for `RUNNING`, then report its VNIC and IP addresses;
- optionally verify SSH and first-boot behavior as `alarm`; and
- optionally delete only the temporary object uploaded by this deployment.

The first version will not create a VCN, subnet, gateway, route table, security
list, Network Security Group, IAM policy, or OCI API signing key. Those are
security-sensitive tenancy decisions and must remain explicit prerequisites.

## Interface

Placement overrides for unattended and cross-compartment use:

```text
--compartment-id OCID
--subnet-id OCID
--availability-domain NAME
```

Important optional arguments:

```text
--profile DEFAULT
--config-file PATH
--region REGION
--bucket NAME | --create-bucket NAME
--object-compartment-id OCID
--instance-name archlinuxarm-a1
--image-name Arch-Linux-ARM-OCI
--shape VM.Standard.A1.Flex
--ocpus 1
--memory-gbs 6
--boot-volume-gbs 50
--assign-public-ip | --no-public-ip
--download-dir PATH
--state-file PATH
--resume
--reuse-download
--reuse-object
--verify-ssh
--ssh-key PATH
--cleanup-object
--dry-run
--verbose
```

The OCI profile defaults to `DEFAULT`, and its configured region is used unless
overridden. Without placement overrides, discover suitable subnets through OCI,
derive their compartment, and probe availability domains for A1. Resume
recovers the selected values from deployment state.

When neither bucket option is supplied, use `BUCKET_NAME` as the name of a
bucket to create. If it is also unset, propose the username-based default and
require interactive confirmation.

Only `VM.Standard.A1.Flex` should be accepted initially. Supporting other shapes
would require defining and testing architecture compatibility rather than
silently treating the image as portable.

`--dry-run` may create the dedicated local SSH key pair, authenticate, inspect
existing resources, validate the release, and print a redacted plan. It must
not create, upload, import, modify, launch, or delete OCI resources.

## Deployment state

The tool will write a mode-`0600` JSON state file atomically after every cloud
mutation. It will contain:

- schema version, deployment UUID, timestamps, profile, and region;
- release filename, SHA-256, upstream MD5, and local verified paths;
- compartment, subnet, and availability-domain identifiers;
- namespace, bucket, object, checksums, ETag, and ownership flags;
- custom-image OCID, lifecycle state, capabilities, and compatible shapes;
- instance OCID, lifecycle state, shape configuration, VNIC, and IP addresses;
- the SSH public-key fingerprint, never private-key contents;
- the last completed phase, known work-request OCIDs, and pending cleanup; and
- a concise failure record suitable for `--resume`.

Display names are not identifiers. Resume and cleanup decisions must primarily
use recorded OCIDs plus a deployment UUID and release SHA-256 stored as OCI
free-form tags.

## Operation sequence

### 1. Validate locally and in OCI

- Require Python 3.8+, `curl`, `oci`, and `ssh-keygen`.
- Generate the dedicated key pair when both files are absent, reuse it when
  complete, and reject partial or mismatched pairs.
- Validate CLI authentication with a read-only namespace request.
- Resolve the effective profile and region without logging credentials.
- Search accessible subnet resources, retrieve their current configuration,
  and filter them for lifecycle and public-IP compatibility.
- Automatically select an unambiguous subnet or present recognizable numbered
  choices; derive the deployment compartment from it unless overridden.
- Probe availability domains for A1 and automatically select an unambiguous
  result or offer a deterministic default.
- Accept explicit CLI and uppercase environment-variable overrides for
  unattended and cross-compartment use.
- Validate the compartment, subnet, availability domain, and generated or
  reused SSH public key.
- Confirm `VM.Standard.A1.Flex` is offered in the selected availability domain
  and validate requested OCPU and memory values against its constraints.
- Confirm public-IP assignment is compatible with the selected subnet.
- Refuse to overwrite an active state file unless `--resume` is supplied.

### 2. Obtain the release

- Run the checked-in downloader into a new directory.
- Reuse an existing download only with `--reuse-download` and only after
  revalidating `build-info.json`, the checksum file, and the QCOW2 bytes.
- Record the release SHA-256 before creating OCI resources.

### 3. Stage the QCOW2 in Object Storage

- Discover the Object Storage namespace.
- Validate an existing private Standard-tier bucket, or create the exact bucket
  selected by an option, `BUCKET_NAME`, or confirmed default.
- Refuse to overwrite an object by default.
- Reuse an object only when explicitly requested and its identity can be
  validated against the local image.
- Upload with checksum verification and visible multipart progress.

### 4. Import and validate the custom image

- Import with the Object Storage tuple form, `QCOW2`, `Linux`, and
  `PARAVIRTUALIZED`.
- Record the image OCID immediately.
- Poll with a bounded timeout, printing lifecycle transitions rather than every
  identical response.
- After `AVAILABLE`, retrieve the global capability schema and any
  image-specific schema. Calculate the effective values and require:

  ```text
  Compute.Firmware = UEFI_64
  Compute.LaunchMode = PARAVIRTUALIZED
  Network.AttachmentType = PARAVIRTUALIZED
  Storage.BootVolumeType = PARAVIRTUALIZED
  ```

- Add `VM.Standard.A1.Flex` compatibility if it is absent, then verify it.
- Do not create or modify an image-specific capability schema automatically in
  the initial version. Fail with a precise corrective command if effective
  capabilities conflict.

### 5. Launch and inspect the instance

- Launch with an explicit source image, shape configuration, boot-volume size,
  subnet, public-IP choice, and the contents of the SSH public key.
- Never read or transmit the SSH private key during launch.
- Record the instance OCID immediately and poll until `RUNNING`.
- Resolve the primary VNIC, private IP, and optional public IP.
- Print an exact `ssh -i ... alarm@...` command when a usable address exists.

### 6. Optional guest verification

When `--verify-ssh` is requested:

- use the private half of the managed key pair;
- use a dedicated known-hosts file rather than disabling host-key checking;
- wait for SSH with a bounded timeout; and
- verify cloud-init completion, passwordless sudo, locked passwords, essential
  services, Oracle datasource use, and root-filesystem growth.

SSH verification should report failures without automatically destroying the
instance, image, or diagnostic evidence.

### 7. Cleanup

- Delete the uploaded object only after the image is `AVAILABLE` and the
  instance is `RUNNING`, and only when the object was uploaded by this run or
  the user explicitly authorized deletion.
- Treat only an OCI `404 NotAuthorizedOrNotFound` response as proof that an
  object is absent. Authentication, authorization, throttling, and network
  errors must remain errors.
- Never delete a pre-existing bucket.
- Delete a run-created bucket only through a separate future opt-in after
  verifying that it is empty.
- Never terminate an instance or delete a custom image on ordinary failure.

## Idempotency and recovery

Every create-like request must use an OCI client request ID when supported.
After an ambiguous timeout or connection failure, the tool must reconcile the
state file and uniquely tagged resources before retrying. It must not blindly
repeat an upload, image import, or instance launch.

`--resume` will verify every completed step against OCI and continue at the
first incomplete phase. It must stop if immutable inputs differ, including the
release SHA-256, compartment, subnet, availability domain, image OCID, shape
configuration, or SSH public-key fingerprint.

On signals or exceptions, persist known OCIDs and the current phase before
exiting. Print exact resume and manual diagnostic commands.

## Documentation design

The documentation should be organized around two paths:

1. **Preparation** — OCI CLI authentication, IAM, networking, service limits,
   and the OCI values the deployer will request.
2. **Deployment** — dry-run, one representative deployment command,
   progress/state behavior, connection, resume, troubleshooting, and teardown.

The guide must include:

- a narrowly scoped IAM policy example and a warning that identity-domain and
  multi-compartment tenancies require adjusted policy names/scopes;
- the 50 GB minimum custom boot-volume size;
- public versus private subnet requirements;
- the `alarm` SSH username and absence of password login;
- the distinction between the OCI API key and instance SSH key;
- safe Object Storage cleanup; and
- serial-console and work-request diagnostics.

Installation instructions should link to Oracle's supported OCI CLI setup and
avoid presenting `curl | bash` as the preferred copy-paste command.

## Offline tests

All default tests must avoid network access and OCI credentials. Mock the OCI
CLI subprocess boundary and cover:

- argument conflicts and numeric bounds;
- OCID and SSH-key validation;
- subnet discovery, filtering, numbered selection, and compartment derivation;
- A1 availability-domain discovery and deterministic default selection;
- OCI JSON parsing and malformed responses;
- command construction without shell interpolation;
- release verification and reuse rules;
- lifecycle success, failure, timeout, and interruption;
- global plus image-specific capability resolution;
- compatible-shape detection;
- atomic state writes and mode `0600`;
- resume mismatch detection;
- object ownership and deletion decisions;
- `404` versus authorization/network cleanup errors;
- redaction in normal, verbose, dry-run, and JSON progress output; and
- refusal to perform mutations during `--dry-run`.

The deployment module should use argument arrays with `subprocess`, never
shell-built command strings. Tests should assert that private-key contents,
OCI config contents, tokens, and passphrases cannot enter logs or state.

## Real OCI acceptance test

Use a disposable deployment ID and the latest published release:

1. Run `--dry-run` and confirm that OCI contains no new resources.
2. Deploy a 1-OCPU, 6-GB `VM.Standard.A1.Flex` instance with a 50-GB boot
   volume.
3. Confirm import reaches `AVAILABLE` with effective UEFI/paravirtualized
   capabilities and A1 compatibility.
4. Confirm the instance reaches `RUNNING` and SSH accepts only the supplied key
   for `alarm`.
5. Run the first-boot checks and confirm the root filesystem expanded.
6. Re-run with `--resume` and verify that no duplicate resources are created.
7. Confirm temporary-object cleanup does not remove the custom image.
8. Manually terminate the instance and delete the custom image after recording
   the results.

If OCI rejects the UEFI/GPT image, preserve the image import state, work-request
logs, and serial-console output. Correct the image or its explicit capability
schema based on that evidence; do not weaken the local smoke test or silently
fall back to an x86/BIOS configuration.

## Implementation order

1. Add the OCI command runner, typed response validation, progress reporting,
   and atomic state-file layer.
2. Add argument parsing and read-only prerequisite validation.
3. Integrate verified release download/reuse.
4. Implement Object Storage upload and ownership tracking.
5. Implement image import, lifecycle waiting, capabilities, and A1
   compatibility.
6. Implement instance launch, lifecycle waiting, and VNIC discovery.
7. Add optional SSH verification and conservative object cleanup.
8. Write the concise deployment guide and README link.
9. Run offline tests and static checks.
10. Perform the real OCI acceptance test and update the support statement with
    the observed result.

No implementation or documentation should be called complete before step 10.
