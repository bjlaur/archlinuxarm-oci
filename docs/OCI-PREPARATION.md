# Prepare OCI for Arch Linux ARM deployment

This is part 1 of the end-to-end deployment guide. It prepares an Oracle Cloud
Infrastructure tenancy and finishes with the values required by
[`deploy-oci.py`](../deploy-oci.py). It does not import a custom image or launch
an instance.

After completing this guide, continue with
[Deploy on Oracle Cloud Infrastructure](OCI-DEPLOYMENT.md).

## What you will prepare

- an OCI CLI profile and API signing key;
- a compartment for the image, instance, and boot volume;
- permission to use the required OCI services;
- a VCN and public subnet suitable for SSH;
- an availability domain offering `VM.Standard.A1.Flex`.

The deployment tool handles the private Object Storage import bucket and a
dedicated instance SSH key pair when deployment begins. It intentionally does
not create compartments, IAM policy, networks, gateways, security rules, or
OCI API signing keys.

This walkthrough uses a public subnet for the first deployment. A private
subnet is also supported, but the machine running the deployer must then have a
working route through OCI Bastion, VPN, FastConnect, or a jump host.

## 1. Choose a region

Choose the OCI region where the bucket, custom image, boot volume, and instance
will live. A1 limits, capacity, and most of these resources are regional.

Keep the OCI Console set to this region while preparing the deployment. Select
the same region when `oci setup config` asks. The deployment tool uses the
region from that profile by default.

## 2. Create or choose a compartment

A dedicated compartment makes IAM policy, costs, and cleanup easier:

1. In the OCI Console, open **Identity & Security** and select
   **Compartments**.
2. Select **Create compartment**.
3. Name it, for example, `ArchLinuxARM`.
4. Create it below the intended parent compartment.
5. Confirm that its lifecycle state becomes **Active**.

An existing compartment or the tenancy root can also be used. When the selected
subnet belongs to this compartment, the deployment tool discovers the
compartment automatically. A separate deployment compartment can still be
selected with an advanced override.

## 3. Configure the OCI CLI

Install the [current OCI
CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm). A
userland `pipx` installation is sufficient:

```bash
pipx install oci-cli
oci --version
```

Create the CLI profile and API signing key:

```bash
oci setup config
```

The setup asks for the tenancy OCID, user OCID, region, and an API private-key
location. The two identity values have different scopes:

- The **tenancy OCID** identifies the entire OCI account. It normally begins
  with `ocid1.tenancy.` and is shared by profiles for users in that tenancy.
  In the OCI Console, open **Profile**, then **Tenancy: _tenancy name_**, and
  copy the tenancy OCID.
- The **user OCID** identifies the individual OCI identity whose API key will
  sign the CLI requests. It normally begins with `ocid1.user.` and must belong
  to the same user where you upload the generated API public key. In the OCI
  Console, open **Profile**, select **My profile**, and copy the user OCID.

Neither OCID is a password or secret. `oci setup config` writes them to a
profile in `~/.oci/config` and creates an RSA API key pair. Protect the private
key because it authenticates CLI requests as this user. The resulting profile
contains entries similar to:

```ini
[DEFAULT]
user=ocid1.user.oc1..REPLACE_ME
tenancy=ocid1.tenancy.oc1..REPLACE_ME
region=us-ashburn-1
key_file=/home/your-user/.oci/oci_api_key.pem
fingerprint=REPLACE_ME
```

Upload the generated **API public key** to the same OCI user:

1. Open the user profile in the OCI Console.
2. Open **API keys** or **Token and keys**.
3. Select **Add API key**.
4. Upload or paste the generated public PEM key.
5. Confirm its fingerprint matches the profile in `~/.oci/config`.

The exact private-key path is the `key_file` value in the profile; do not assume
a particular filename. The OCI API key is unrelated to the OpenSSH key that
the deployment tool manages for instance login.

The deployment tool uses the `DEFAULT` profile unless `--profile` selects
another one. Verify authentication with a read-only request:

```bash
oci os ns get
```

## 4. Grant deployment permissions

If the user belongs to the built-in `Administrators` group, its tenancy-wide
policy already covers the deployment. Do not add a redundant policy.

For narrower access, create a group such as `ArchImageDeployers`, add the user,
and create a policy in the tenancy root. Replace the group and compartment
names:

```text
Allow group ArchImageDeployers to manage instances in compartment ArchLinuxARM
Allow group ArchImageDeployers to manage instance-images in compartment ArchLinuxARM
Allow group ArchImageDeployers to use volume-family in compartment ArchLinuxARM
Allow group ArchImageDeployers to use virtual-network-family in compartment ArchLinuxARM
Allow group ArchImageDeployers to manage object-family in compartment ArchLinuxARM
Allow group ArchImageDeployers to inspect work-requests in compartment ArchLinuxARM
Allow group ArchImageDeployers to inspect compute-image-capability-schema in compartment ArchLinuxARM
Allow group ArchImageDeployers to read compute-global-image-capability-schema in tenancy
Allow group ArchImageDeployers to read app-catalog-listing in tenancy
```

Groups outside the default identity domain need the domain-qualified subject
syntax selected by OCI's policy builder. If networking or Object Storage lives
in a different compartment, change the corresponding statement's scope.

The policy permits deployment operations; it does not grant permission to
create the policy itself. Ask a tenancy administrator when necessary. IAM
changes may take a short time to propagate.

Oracle documents the resource types in [Details for the Core
Services](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/corepolicyreference.htm)
and [Details for Object
Storage](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/objectstoragepolicyreference.htm).

## 5. Create or choose a VCN and public subnet

For a new tenancy, use OCI's **Create a VCN with Internet Connectivity**
wizard:

1. Open **Networking** and select **Virtual cloud networks**.
2. Select **Start VCN Wizard**.
3. Select **Create VCN with Internet Connectivity**.
4. Choose the deployment compartment.
5. Accept or customize the non-overlapping CIDRs.
6. Create the VCN.

The wizard creates public and private regional subnets plus the necessary
Internet, NAT, and Service gateways. Select the regional public subnet for the
first deployment.

Before using any public subnet, confirm:

- `prohibit-public-ip-on-vnic` is `false`;
- its route table sends `0.0.0.0/0` to an enabled Internet Gateway;
- a subnet security list permits stateful TCP destination port 22 from your
  public source address, preferably a `/32`; and
- egress traffic is permitted.

The initial deployment tool does not accept an NSG OCID. An NSG rule protects
the instance only when that NSG is explicitly attached to its VNIC, so the
first deployment must rely on a security list attached to the selected subnet.

The deployment tool searches OCI for accessible subnets in the configured
region. It selects the subnet automatically when only one is suitable for the
requested public/private IP mode, or displays a numbered choice when several
qualify. Neither the subnet nor VCN OCID needs to be copied.

If networking is in a separate compartment, use that compartment when listing
VCNs and subnets and in the IAM networking policy.

## 6. Confirm A1 is offered in the region

The deployment tool lists the tenancy's availability domains through OCI and
probes each one for `VM.Standard.A1.Flex`. It selects the domain automatically
when only one qualifies. When several qualify, it shows their names and offers
the first sorted name as the default.

Seeing the shape through the API proves it is offered, not that host capacity
is currently available. An `Out of host capacity` launch error may require
selecting another availability domain or retrying later.

## 7. Check service limits

In **Governance & Administration**, open **Limits, Quotas and Usage** for the
selected region. Check:

- Ampere A1 OCPUs and memory;
- Compute instance count;
- custom-image count;
- boot-volume count and storage; and
- VNIC and public IPv4 limits when assigning a public IP.

The default deployment uses 1 OCPU, 6 GB of memory, and a 50 GB boot volume.
Custom images and boot volumes remain until explicitly deleted and can incur
storage charges.

## Preparation checklist

- [ ] `oci os ns get` succeeds with the intended profile and region.
- [ ] The deploying identity has the required policy.
- [ ] The subnet permits public IPs and routes through an Internet Gateway.
- [ ] A subnet security list allows TCP/22 from the administrator's address.
- [ ] The selected region offers A1 Flex.
- [ ] Relevant service limits have been checked.

No deployment OCIDs need to be copied. The deployment tool discovers the
network placement and A1-capable availability domains before creating
anything.

Continue with [OCI-DEPLOYMENT.md](OCI-DEPLOYMENT.md) to verify the release,
import the custom image, launch the instance, and test SSH.
