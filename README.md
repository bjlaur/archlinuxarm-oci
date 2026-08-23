# Arch Linux ARM for Oracle Cloud Infrastructure

Community-built Arch Linux ARM AArch64 images for Oracle Cloud Infrastructure
Ampere A1 (`VM.Standard.A1.Flex`). The images start from the signed official
Arch Linux ARM generic AArch64 root filesystem.

This is not an official Arch Linux, Arch Linux ARM, or Oracle image.

## Download the latest image

The easiest option downloads the current QCOW2, its checksum, and build
metadata, then verifies that all three agree:

```bash
curl -fsSLO https://raw.githubusercontent.com/bjlaur/archlinuxarm-oci/main/download-latest.py
python3 download-latest.py
```

Requirements: Python 3.8 or newer and `curl`. Pass a directory to download
somewhere other than the current directory:

```bash
python3 download-latest.py ~/Downloads/archlinuxarm-oci
```

You can instead download the QCOW2, matching `.sha256` file, and
`build-info.json` from the [latest GitHub
Release](https://github.com/bjlaur/archlinuxarm-oci/releases/latest), then run:

```bash
sha256sum -c -- *.qcow2.sha256
```

Published artifacts also carry signed GitHub Actions provenance. With the
[GitHub CLI](https://cli.github.com/), verify that the exact QCOW2 was produced
by this repository's workflow:

```bash
gh attestation verify *.qcow2 --repo bjlaur/archlinuxarm-oci
```

## Deploy it automatically

`deploy-oci.py` is the experimental happy-path deployer. It uses the official
OCI CLI to download and verify the release, upload it to Object Storage, import
it as a custom image, launch an A1 instance, and optionally verify first boot
over SSH.

First clone the repository and configure the OCI CLI:

```bash
git clone https://github.com/bjlaur/archlinuxarm-oci.git
cd archlinuxarm-oci
pipx install oci-cli
oci setup config
oci os ns get
```

Your OCI account also needs a compartment, suitable VCN/subnet, and the
required IAM permissions. The [OCI preparation
guide](docs/OCI-PREPARATION.md) walks through that one-time setup.

Run the read-only OCI check first. It still downloads the image and creates a
local instance SSH key if needed, but it does not create or modify OCI
resources:

```bash
./deploy-oci.py --assign-public-ip --dry-run
```

Then reuse that verified download, launch the instance, verify SSH, and remove
the temporary import object and bucket after success:

```bash
./deploy-oci.py --assign-public-ip --reuse-download --verify-ssh --cleanup-bucket
```

The tool interactively discovers a subnet and A1-capable availability domain
when there is more than one choice. It defaults to 1 OCPU, 6 GB RAM, and a 50
GB boot volume. Use `--no-public-ip` instead when the machine running the
deployer can reach a private subnet.

After launch:

```bash
ssh -i ~/.ssh/archlinuxarm-oci alarm@INSTANCE_IP
```

The image build installs its small set of required packages after refreshing
the package databases, but deliberately does not perform the much slower full
system upgrade. Bring the new instance fully current soon after first login:

```bash
sudo pacman -Syu
```

Until that command completes, the instance is technically in a partial-upgrade
state. Do the upgrade before installing additional packages or treating the
instance as long-lived.

If a deployment is interrupted, keep `.deploy-oci-state.json` and rerun the
same command with `--resume`. See the [full OCI deployment
guide](docs/OCI-DEPLOYMENT.md) before using overrides, resuming, cleaning up a
partial deployment, or deploying manually.

## Image defaults

| Setting | Default |
| --- | --- |
| Architecture | AArch64 |
| Boot | GPT + UEFI |
| OCI launch mode | Paravirtualized |
| Login user | `alarm` |
| SSH authentication | OCI-provided public key only |
| Root login | Locked; root SSH disabled |
| `alarm` sudo | Passwordless |
| Source disk | 4 GiB, expanded on first boot |

The published image contains no baked-in SSH key, persistent SSH host key,
machine identity, random seed, or retained cloud-init instance state. OCI
network security lists or NSGs remain responsible for inbound access.

## More documentation

- [Technical guide](docs/README.md): image layout, boot flow, verification,
  security model, deployment state, and troubleshooting.
- [Prepare OCI](docs/OCI-PREPARATION.md): CLI, IAM, compartment, network, and
  A1 prerequisites.
- [Detailed OCI deployment](docs/OCI-DEPLOYMENT.md): automation, manual import,
  resume, cleanup, networking, and diagnostics.
- [Developer guide](DEVELOPERS.md): dependencies, build architecture, local
  builds, tests, CI, and releases.
- [Code review](docs/CODE-REVIEW.md): prioritized findings from the full
  repository review.
- [Maintainer handoff](CODEX-HANDOFF.md): review scope, implemented changes,
  validation, and remaining work.

License: [MIT](LICENSE).
