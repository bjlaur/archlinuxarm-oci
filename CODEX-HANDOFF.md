# Codex handoff: deploy the image from a faster server

## Objective

Complete the first real Oracle Cloud Infrastructure deployment of the published
Arch Linux ARM QCOW2 from a server with faster download and upload bandwidth.
Use the repository's `deploy-oci.py`; do not reimplement the deployment with
ad-hoc OCI commands unless diagnosing a failure.

## Start here

```bash
git clone https://github.com/bjlaur/archlinuxarm-oci.git
cd archlinuxarm-oci
pipx install oci-cli
oci setup config
```

Register the generated OCI API public key with the same OCI user. Complete any
missing IAM or network preparation in
[`docs/OCI-PREPARATION.md`](docs/OCI-PREPARATION.md). Never copy OCI API private
keys, OpenSSH private keys, or deployment state into Git.

The deployer defaults to the OCI CLI's `DEFAULT` profile and its configured
region. It discovers accessible subnets through OCI Resource Search, filters
them for the requested public/private IP mode, derives the deployment
compartment from the selected subnet, and probes availability domains for
`VM.Standard.A1.Flex`. Explicit placement flags remain available for unusual
cross-compartment layouts.

## Fast manual image download

The built-in downloader uses curl. For a faster segmented transfer of the
current release, place all three release assets in the deployer's default
download directory:

```bash
mkdir -p archlinuxarm-oci-download

aria2c -c -x 10 -s 10 \
  -d archlinuxarm-oci-download \
  https://github.com/bjlaur/archlinuxarm-oci/releases/download/2026.08.22-27e2fea-23eec863/archlinuxarm-oci-aarch64-2026.08.22-27e2fea-23eec863.qcow2

curl -fL -o archlinuxarm-oci-download/build-info.json \
  https://github.com/bjlaur/archlinuxarm-oci/releases/download/2026.08.22-27e2fea-23eec863/build-info.json

curl -fL \
  -o archlinuxarm-oci-download/archlinuxarm-oci-aarch64-2026.08.22-27e2fea-23eec863.qcow2.sha256 \
  https://github.com/bjlaur/archlinuxarm-oci/releases/download/2026.08.22-27e2fea-23eec863/archlinuxarm-oci-aarch64-2026.08.22-27e2fea-23eec863.qcow2.sha256
```

If a newer release exists, use the `.qcow2`, matching `.qcow2.sha256`, and
`build-info.json` from that same release instead. `--reuse-download` performs a
full local verification against both published checksum sources before OCI is
allowed to use the image.

## Validate, then deploy

Run the OCI-read-only check first:

```bash
./deploy-oci.py --assign-public-ip --reuse-download --dry-run
```

Dry-run may generate the dedicated local Ed25519 pair at
`~/.ssh/archlinuxarm-oci`, but it does not create or modify OCI resources. With
no explicit bucket option, accept or reject the proposed private Standard-tier
bucket name interactively. The tool asks separately whether to delete that
bucket after a successful run; the default answer is No.

After dry-run succeeds:

```bash
./deploy-oci.py \
  --assign-public-ip \
  --reuse-download \
  --verify-ssh \
  --cleanup-object
```

Defaults are `VM.Standard.A1.Flex`, 1 OCPU, 6 GB memory, and a 50 GB boot
volume. The real run uploads the verified QCOW2, imports and validates the
custom image, launches the instance, waits for first boot, verifies SSH and
guest security, and removes only its temporary Object Storage object.

See [`docs/OCI-DEPLOYMENT.md`](docs/OCI-DEPLOYMENT.md) for overrides,
cross-compartment deployment, private networking, lifecycle behavior,
troubleshooting, and eventual manual teardown.

## Resume and failure handling

The real deployment writes `.deploy-oci-state.json` with mode `0600`. On a
recoverable interruption, rerun the real command with `--resume`; placement and
bucket selections are recovered from state:

```bash
./deploy-oci.py \
  --assign-public-ip \
  --verify-ssh \
  --cleanup-object \
  --resume
```

Do not delete the state file during recovery. The deployer deliberately leaves
the instance, image, and diagnostic evidence intact on failure. It never
automatically terminates an instance or deletes a custom image.

To discard a partial deployment and start fresh, run `./deploy-oci.py --clean`.
It deletes only resources recorded in `.deploy-oci-state.json`, aborts recorded
bucket multipart uploads, and removes the state file after successful cleanup.

## Current validation status

- The local default suite passes 101 tests.
- The optional installed-OCI-CLI command-surface suite passes 5 tests.
- Real read-only OCI discovery was exercised successfully: it found the sole
  suitable public subnet, derived its compartment, and found three A1-capable
  availability domains in the configured Chicago region.
- No real OCI image import or instance launch has been completed yet. That is
  the remaining acceptance test.

Useful checks:

```bash
python3 -m unittest discover -s tests -v
python3 tests/excluded_test_oci_cli.py -v
python3 -m py_compile deploy-oci.py download-latest.py build.py
```
