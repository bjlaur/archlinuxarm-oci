# Technical guide

This guide explains what the published image contains and how the download,
boot, deployment, and cleanup paths fit together. Start with the short
[README](../README.md) if you only want the current image or a happy-path OCI
deployment.

## Project scope and support

The project converts the official Arch Linux ARM generic AArch64 root
filesystem into a GPT/UEFI QCOW2 suitable for OCI Ampere A1. It adds the
bootloader, cloud-init, networking, SSH hardening, SSHGuard, and automatic root
filesystem expansion required for a practical factory image.

The result is community-built and community-tested. Arch Linux ARM does not
publish it, and Oracle does not list Arch Linux ARM as a supported platform
image. OCI service limits, A1 capacity, pricing, and Always Free eligibility
are properties of the user's tenancy rather than guarantees made by this
project.

## Release artifacts and verification

Each release contains three files:

| Artifact | Purpose |
| --- | --- |
| `*.qcow2` | Compressed AArch64 disk image |
| `*.qcow2.sha256` | SHA-256 and exact image filename |
| `build-info.json` | Build commit, upstream identity, signer fingerprint, image identity, and acceleration modes |

`download-latest.py` fetches the metadata and checksum before downloading the
large image. It rejects unsafe filenames and inconsistent checksums, downloads
to a temporary file, hashes the bytes, and publishes the files without
overwriting existing paths.

The checksum file and `build-info.json` are consistency checks published in
the same GitHub Release; they are not two independent signatures. The upstream
Arch Linux ARM root filesystem is separately authenticated during the build by
its detached OpenPGP signature and a pinned full signing-key fingerprint.

The publish job also creates a signed GitHub artifact attestation covering the
QCOW2, checksum file, and `build-info.json`. The attestation binds their exact
digests to this repository, commit, and GitHub Actions workflow identity. It
does not assert that the source code is bug-free, but it prevents a replaced
release asset from passing provenance verification merely because its adjacent
checksum was replaced too.

To verify a manual download:

```bash
sha256sum -c -- *.qcow2.sha256

python3 -c '
import json
info = json.load(open("build-info.json"))
print("{}  {}".format(info["image_sha256"], info["image_filename"]))
' | sha256sum -c -

gh attestation verify *.qcow2 --repo bjlaur/archlinuxarm-oci
```

## Disk and boot layout

The source disk is 4 GiB:

| Partition | Filesystem | Mount point | Purpose |
| --- | --- | --- | --- |
| 1 | FAT | `/boot/efi` | ARM64 UEFI fallback loader (`BOOTAA64.EFI`) |
| 2 | ext4 | `/` | Arch Linux ARM root filesystem |

GRUB boots `/boot/Image` and `/boot/initramfs-linux.img` with a serial console
on `ttyAMA0`. The build creates a generic initramfs rather than carrying
hardware autodetection from the build host into the image.

On an OCI boot volume larger than the source disk, `oci-grow-root.service`
runs `growpart` for partition 2 and then performs an online `resize2fs`. A
marker at `/var/lib/oci-root-grown` prevents unnecessary later runs. OCI custom
images require a boot volume of at least 50 GB, which is also the deployer's
default.

## First boot

The factory image uses cloud-init's Oracle datasource. At first boot it:

1. obtains instance metadata from OCI;
2. installs the launch-time SSH public key for the existing `alarm` user;
3. applies supported hostname and user-data metadata;
4. generates the machine identity, random seed, and SSH host keys that were
   deliberately removed before publication; and
5. starts the normal network, SSH, SSHGuard, and root-growth services.

The exact local SSH private key used by `deploy-oci.py` never enters OCI
metadata or deployment state. OCI receives only its `.pub` half.

## Accounts and security model

| Account | Console password | SSH | sudo |
| --- | --- | --- | --- |
| `root` | Locked | Disabled | n/a |
| `alarm` | Upstream `alarm` password retained for console recovery | Public key only | Passwordless |

SSH password and keyboard-interactive authentication are disabled in factory
images. Root SSH is disabled. The image contains no authorized key, persistent
host key, machine ID, random seed, or retained cloud-init instance directory.

The known upstream `alarm` password is not accepted by SSH, but it remains a
deliberate console-recovery tradeoff. Access to the OCI serial console should
therefore be protected with narrow IAM policy. Change the password after first
boot if you do not want this recovery path.

The image enables SSHGuard with an nftables backend for repeated SSH failures.
It does not install a static host firewall policy. Use an OCI security list or
Network Security Group to expose only required ports.

## What the experimental deployer does

The deployer orchestrates the installed `oci` command; it does not use the OCI
Python SDK. Its phases are:

1. discover or validate the subnet, compartment, availability domain, and A1
   shape constraints;
2. generate or validate a dedicated Ed25519 instance key pair;
3. download and revalidate the current release;
4. create or select a private Standard Object Storage bucket;
5. upload the QCOW2 without overwriting an object;
6. import a paravirtualized custom image;
7. set and verify UEFI/paravirtualized capability defaults and A1 shape
   compatibility;
8. launch the instance and wait for `RUNNING`;
9. report VNIC addresses and optionally verify the guest over SSH; and
10. optionally remove the temporary object and bucket.

It intentionally does not create IAM policy, compartments, VCNs, subnets,
gateways, route tables, security rules, NSGs, or OCI API signing keys.

### Local and OCI keys are different

- The OCI API private key referenced by `~/.oci/config` signs OCI API calls.
- The OpenSSH key under `~/.ssh/archlinuxarm-oci` logs in to the new instance.

Neither key substitutes for the other. Do not commit either private key.

### State, resume, and cleanup

Real deployments write `.deploy-oci-state.json` atomically with mode `0600`.
It records immutable inputs, release identity, deployment UUID, resource OCIDs,
ownership flags, lifecycle states, addresses, and the last failure. It does not
record private-key contents.

Resume requires the same immutable settings and the same verified release:

```bash
./deploy-oci.py SAME_OPTIONS --resume
```

The first Ctrl-C requests a graceful checkpoint after the current OCI command.
A second Ctrl-C exits immediately and may leave an OCI request in an ambiguous
state.

Explicit cleanup uses the state file as a manifest:

```bash
./deploy-oci.py --clean
```

Cleanup uses the recorded profile, config path, and region unless explicitly
overridden. It terminates only an instance marked as created by the deployment,
deletes only a custom image marked as created, deletes only an object marked as
uploaded, and deletes only a bucket marked as created. Bucket deletion still
requires the bucket to be empty. Reused objects and buckets are retained.

Do not delete the state file before recovery or cleanup is complete. Because
the state file authorizes destructive cleanup actions, protect it from
modification as well as disclosure.

## Manual OCI settings

For a manual Console or CLI import, use:

```text
Image type:                QCOW2
Operating system:          Linux
Launch mode:               PARAVIRTUALIZED
Compute.Firmware:          UEFI_64
Compute.LaunchMode:        PARAVIRTUALIZED
Network.AttachmentType:    PARAVIRTUALIZED
Storage.BootVolumeType:    PARAVIRTUALIZED
Compatible shape:          VM.Standard.A1.Flex
Boot volume:               at least 50 GB
Login user:                alarm
```

See [OCI-DEPLOYMENT.md](OCI-DEPLOYMENT.md) for the complete Console and CLI
procedure.

## Networking

For direct SSH, use a subnet that permits public IP assignment, routes
`0.0.0.0/0` through an Internet Gateway, and has a security-list rule allowing
TCP/22 from the administrator's source address. The deployer does not attach an
NSG.

Private instances are supported with `--no-public-ip`, but SSH verification
requires a route from the machine running the deployer through Bastion, VPN,
FastConnect, or a jump host.

## Troubleshooting map

| Symptom | First checks |
| --- | --- |
| `NotAuthorizedOrNotFound` | OCI profile, compartment scope, IAM propagation |
| `Out of host capacity` | Another availability domain or a later retry |
| SSH timeout | Public/private route, security list, source CIDR, port 22 |
| `RUNNING` but no boot | OCI serial-console output |
| Small root filesystem | `journalctl -u oci-grow-root.service -b`, `lsblk`, `df -hT /` |
| Interrupted deploy | Preserve state and rerun with `--resume` |

The detailed deployment guide includes work-request commands and teardown
instructions.

## Documentation map

- [Project README](../README.md): shortest download and deployment path.
- [OCI preparation](OCI-PREPARATION.md): tenancy, CLI, IAM, and networking.
- [OCI deployment](OCI-DEPLOYMENT.md): full automation and manual procedure.
- [Developer guide](../DEVELOPERS.md): build and maintenance workflow.
- [Code review](CODE-REVIEW.md): prioritized implementation findings.
- [OCI smoke CI plan](OCI-SMOKE-CI-PLAN.md): deferred proposal for real-OCI
  acceptance testing from GitHub Actions.
