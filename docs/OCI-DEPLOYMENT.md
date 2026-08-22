# Deploy on Oracle Cloud Infrastructure

This is part 2 of the end-to-end guide. It starts with a configured OCI tenancy
and finishes with the latest published Arch Linux ARM image running and
verified on an OCI `VM.Standard.A1.Flex` instance.

If you do not yet have the required OCI profile, compartment, and network,
complete [Prepare OCI for Arch Linux ARM
deployment](OCI-PREPARATION.md) first.

The deployment tool performs these operations:

1. creates or validates a dedicated local SSH key pair;
2. validates OCI access, the subnet, and A1 shape constraints;
3. downloads and verifies the latest GitHub Release;
4. uploads the QCOW2 to a private Object Storage bucket;
5. imports it as a paravirtualized custom image;
6. verifies UEFI and paravirtualized image capabilities;
7. adds `VM.Standard.A1.Flex` image compatibility;
8. launches the instance and waits for `RUNNING`;
9. reports its IP addresses and optionally verifies the guest over SSH; and
10. optionally removes the temporary Object Storage object.

No OCI resource is created during `--dry-run`. The real deployment records
every mutation in a private state file so it can be resumed safely.

## 1. Check the defaults

The tool uses the OCI CLI's `DEFAULT` profile and that profile's configured
region. Run the final local/authentication checks:

```bash
oci --version
oci os ns get
```

The OCI API private key configured in `~/.oci/config` authenticates the CLI.
The deployment tool uses `$HOME/.ssh/archlinuxarm-oci` for instance login. It
generates that dedicated Ed25519 key pair without a passphrase when both files
are absent, with the private key readable only by its owner. It reuses the pair
when both files are present and stops if they are incomplete or mismatched.
Pass `--ssh-key PATH` to use a different private-key location; its public half
is `PATH.pub`.

The tool discovers placement through OCI before making changes:

1. searches for accessible subnets in the configured region;
2. filters out unavailable subnets and, for `--assign-public-ip`, subnets that
   prohibit public IPs;
3. automatically selects the only suitable subnet or displays a numbered list;
4. uses that subnet's compartment for the image and instance; and
5. probes the tenancy's availability domains and selects one offering
   `VM.Standard.A1.Flex`.

When several A1 domains qualify, their sorted names are displayed and the first
is offered as the default. Explicit `--compartment-id`, `--subnet-id`, and
`--availability-domain` options or their uppercase environment-variable
equivalents remain available for unattended or cross-compartment deployments.

## 2. Run the read-only deployment check

From the repository root, run:

```bash
./deploy-oci.py --assign-public-ip --dry-run
```

With no bucket option or `BUCKET_NAME` environment variable, the tool proposes
`archlinuxarm-oci-import-$USER` and asks whether to use it. Accepting selects a
private Standard-tier bucket that the real deployment will use, creating it
only if needed. It then asks whether to delete that bucket after successful
deployment if it is empty; the default answer is No. Dry-run does not create or
delete any bucket.

The tool also creates the local SSH key pair if necessary, performs read-only
OCI validation, and downloads the latest release. The downloader verifies that
`build-info.json`, the published `.sha256` file, and the SHA-256 of the
downloaded QCOW2 all agree. Expect roughly a 1 GB download.

Dry-run does **not** create the bucket, upload the image, import a custom image,
change shape compatibility, launch an instance, or delete anything. It prints
the mutations that the real deployment would perform.

## 3. Deploy and verify the instance

After dry-run succeeds, run the real deployment. `--reuse-download` tells it to
revalidate and reuse the QCOW2 that dry-run already downloaded:

```bash
./deploy-oci.py \
  --assign-public-ip \
  --reuse-download \
  --verify-ssh \
  --cleanup-object
```

The command can take time during the upload and custom-image import. It prints
stable phases and OCI lifecycle transitions. The OCI CLI supplies multipart
upload progress on an interactive terminal.

The defaults create one A1 OCPU, 6 GB of memory, and a 50 GB boot volume. OCI
requires a custom boot-volume size of at least 50 GB. On first boot,
`oci-grow-root.service` expands the image's 4 GiB partition and ext4 filesystem
to the new volume size.

`--verify-ssh` waits for `alarm` key authentication and checks:

- cloud-init completion;
- passwordless sudo;
- locked root and `alarm` passwords;
- networking, SSH, nftables, and SSHGuard services; and
- successful root-filesystem expansion.

Only the public half of the managed key pair is supplied to OCI. The private
key is passed only to the local `ssh` process; it is never uploaded, placed in
OCI metadata, printed, or written to deployment state.

`--cleanup-object` deletes the temporary QCOW2 object only after the custom
image is `AVAILABLE` and the instance is `RUNNING`. `--cleanup-bucket` also
deletes the selected bucket after a successful deployment, but only after the
temporary object is deleted and the bucket is empty. Neither option deletes the
custom image, instance, or boot volume.

## 4. Confirm the result

A successful run ends with output similar to:

```text
DONE  OCI instance is running
Instance OCID: ocid1.instance.oc1...
Private IP: 10.0.0.123
Public IP: 203.0.113.10
Connect: ssh -i /home/user/.ssh/archlinuxarm-oci alarm@203.0.113.10
State: /path/to/repository/.deploy-oci-state.json
```

Connect manually if desired:

```bash
ssh -i "$HOME/.ssh/archlinuxarm-oci" alarm@PUBLIC_IP
```

Inside the guest, inspect the completed setup:

```bash
cloud-init status --wait --long
sudo -n true
sudo passwd -S root
sudo passwd -S alarm
systemctl is-active systemd-networkd systemd-resolved sshd nftables sshguard
sudo systemctl status oci-grow-root.service --no-pager
test -e /var/lib/oci-root-grown
lsblk -f
df -hT /
```

The root and `alarm` password status should be locked, the services should be
active, and `/` should occupy nearly the selected boot-volume size.

The instance is now ready for normal Arch Linux ARM administration.

## 5. Resume after an interruption or failure

The real deployment writes `.deploy-oci-state.json` atomically with mode
`0600`. It contains the release identity, immutable inputs, deployment UUID,
resource OCIDs, ownership flags, lifecycle states, IP addresses, and the last
failure. It does not contain API or SSH private keys.

Run the same real-deployment command with `--resume` appended:

```bash
./deploy-oci.py \
  --assign-public-ip \
  --verify-ssh \
  --cleanup-object \
  --resume
```

Resume recovers the compartment, subnet, availability domain, and bucket from
the state file, then revalidates the existing download. All immutable settings
and the release SHA-256 must match. Recorded OCIDs are used first; uniquely
tagged images and instances can be reconciled after an ambiguous API timeout.
The tool stops rather than guessing when state differs or multiple resources
match.

Do not delete the state file while recovering a partial deployment.

## More things you should know

### Existing buckets and objects

Set `BUCKET_NAME` to choose a different bucket name without a bucket-name
prompt, or pass `--create-bucket NAME` explicitly. The implicit default and
`BUCKET_NAME` forms use an existing private Standard-tier bucket when present,
or create it when missing. Use `--bucket NAME` for a bucket you created
separately; it must already be private and Standard tier.

The tool refuses to overwrite an object. `--reuse-object` is an explicit
recovery option and accepts an existing object only when its size and release
SHA-256 metadata match.

An object reused from outside the deployment is never eligible for automatic
cleanup. Bucket cleanup is opt-in, defaults to No when prompted, and refuses to
delete a non-empty bucket.

### Public and private networking

`--assign-public-ip` requires a subnet that permits public IPs, an Internet
Gateway route, and a subnet security-list rule allowing TCP/22 from the client.
The script does not currently attach an NSG.

For a private subnet, replace `--assign-public-ip` with `--no-public-ip`.
Automatic SSH verification works only if the machine running the tool can route
to the instance's private address. Otherwise omit `--verify-ssh` and connect
later through Bastion, VPN, FastConnect, or a jump host.

### Changing instance size

`VM.Standard.A1.Flex` is the only supported shape in the initial tool. Change
resources with:

```text
--ocpus NUMBER
--memory-gbs NUMBER
--boot-volume-gbs NUMBER
```

The tool validates OCPU, total-memory, and memory-per-OCPU constraints returned
by OCI before uploading the image. Shape availability does not guarantee
current host capacity.

### Image format and OCI support status

The deployment imports and verifies:

```text
Image type:   QCOW2
OS:           Linux
Launch mode:  PARAVIRTUALIZED
Firmware:     UEFI_64
NIC:          PARAVIRTUALIZED
Boot volume:  PARAVIRTUALIZED
Shape:        VM.Standard.A1.Flex
```

The Arch Linux ARM image is community-built and is not an Oracle platform
image. Oracle's general custom-Linux documentation does not list Arch Linux ARM
among its tested operating systems and still describes BIOS/MBR source-image
requirements. This repository therefore treats OCI deployment as
community-tested rather than Oracle-supported.

### Security model

The published factory image contains no usable password, baked-in SSH key,
persistent host key, machine identity, or retained cloud-init state. OCI passes
the selected public key in instance metadata; cloud-init installs it for the
existing `alarm` account. Root SSH and SSH password authentication remain
disabled.

### Troubleshooting

Inspect the recorded resources:

```bash
python3 -m json.tool .deploy-oci-state.json
oci compute image get --image-id IMAGE_OCID
oci compute instance get --instance-id INSTANCE_OCID
oci compute instance list-vnics --instance-id INSTANCE_OCID
```

List work requests for an image or instance:

```bash
oci work-requests work-request list \
  --compartment-id COMPARTMENT_OCID \
  --resource-id RESOURCE_OCID --all
```

Common causes:

- `NotAuthorizedOrNotFound`: wrong compartment/profile or IAM policy has not
  propagated;
- `Out of host capacity`: select another availability domain or retry later;
- SSH timeout: check public IP assignment, Internet Gateway route, security
  list, source CIDR, and port 22;
- instance `RUNNING` but no boot: inspect an [OCI serial-console
  connection](https://docs.oracle.com/en-us/iaas/Content/Compute/References/serialconsole.htm);
- root filesystem remains small: inspect
  `journalctl -u oci-grow-root.service -b` inside the guest.

The tool preserves diagnostic resources on failure. It never automatically
terminates an instance or deletes a custom image.

### Costs and eventual teardown

The instance, boot volume, custom image, and any bucket remain OCI resources
until deliberately deleted. Review current OCI pricing and your Always Free
eligibility; do not assume every selected resource is free.

Before teardown, copy the exact OCIDs from `.deploy-oci-state.json` and inspect
them. Terminating the instance and deleting the custom image are intentionally
manual operations in the initial version so a failure cannot destroy evidence
or the wrong resource.

### Oracle references

- [Importing Custom
  Images](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/custom-images-import.htm)
- [Importing Custom Linux
  Images](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/importingcustomimagelinux.htm)
- [Editing image
  capabilities](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/configuringimagecapabilities.htm)
- [`oci compute image import
  from-object`](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/compute/image/import/from-object.html)
- [`oci compute instance
  launch`](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/compute/instance/launch.html)
