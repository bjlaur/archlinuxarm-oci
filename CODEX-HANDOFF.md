# Codex handoff: OCI deployment status

## Current state

The repository now has a working `deploy-oci.py` path for Oracle Cloud
Infrastructure Ampere A1. The deployer validates OCI access, downloads and
verifies the release image, uploads it to Object Storage, imports it as a
custom image, applies the required UEFI/paravirtualized capabilities, launches
`VM.Standard.A1.Flex`, verifies SSH as `alarm`, and can clean up temporary
Object Storage resources.

Use [`docs/OCI-PREPARATION.md`](docs/OCI-PREPARATION.md) for one-time tenancy,
IAM, and network setup. Use [`docs/OCI-DEPLOYMENT.md`](docs/OCI-DEPLOYMENT.md)
for automated deployment, manual Console/CLI deployment, resume, cleanup, and
troubleshooting.

## Standard validation

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile deploy-oci.py download-latest.py build.py
```

The optional OCI CLI command-surface check requires installed OCI tooling and
credentials:

```bash
python3 tests/excluded_test_oci_cli.py -v
```

## Deployment reminder

Run dry-run first:

```bash
./deploy-oci.py --assign-public-ip --dry-run
```

Then deploy:

```bash
./deploy-oci.py \
  --assign-public-ip \
  --reuse-download \
  --verify-ssh \
  --cleanup-bucket
```

On interruption or a recoverable OCI error, keep `.deploy-oci-state.json` and
rerun the same command with `--resume`. To intentionally discard a recorded
deployment, use `./deploy-oci.py --clean`; it acts only on resources recorded in
the state file.

Never commit OCI API private keys, OpenSSH private keys, deployment state, or
downloaded image artifacts.
