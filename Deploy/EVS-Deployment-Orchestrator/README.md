# Amazon EVS — Automated VCF 9 Install

Deploy a fully-configured VMware Cloud Foundation 9 environment on Amazon EVS by
launching a single CloudFormation stack and letting the automation run to
completion (~4-7 hours). No Terraform, no manual multi-phase workflow — you fill
in a few parameters, launch one stack, and it builds everything end to end.

What normally takes days of coordinated networking, host provisioning, and VCF
configuration is reduced to filling in a few parameters and launching one stack.

## How it works

In plain terms: you launch **one CloudFormation stack**. That stack starts a
small **"runner" server** in your account, and the runner does all the work for
you — it builds the network, provisions the bare-metal ESXi hosts into your
account, and installs
and configures all of VMware Cloud Foundation (vCenter, NSX, SDDC Manager, VCF
Operations). You don't run any of the steps yourself; you just watch the
progress. It takes about **4-7 hours**, and when it finishes you have a
ready-to-use VCF environment. To remove everything later, you run **one teardown
command**.

## What you get

- A VPC (or bring your own) with all required networking (DNS, NAT, Route Server, security group)
- Configured ESXi hosts on bare-metal EC2 instances in your own account (you choose the instance type and count in your blueprint; the instances run as EVS bare-metal capacity billed to your account)
- VCF 9 fully installed and configured:
  - **vCenter Server** — VMware's centralized management platform
  - **NSX Manager** — software-defined networking and security
  - **SDDC Manager** — VCF lifecycle management
  - **VCF Operations** — monitoring and analytics
- An NSX edge cluster for connectivity between the NSX overlay network and your VPC
- All appliance credentials managed in AWS Secrets Manager
- Full teardown in one command (returns the account to a clean, non-billing state)

> **Already have a VPC?** You can deploy into your existing VPC
> as long as it has an internet gateway and you provide 3 subnets (public,
> service-access, and runner) in the same AZ. See the [BYO-VPC](#byo-vpc)
> section for the exact requirements and setup commands.

## Prerequisites

Before you start, make sure you have:

1. **AWS account** with an active Business or Enterprise Support plan. To
   check from the CLI, run `aws support describe-severity-levels --region us-east-1`:
   a list of severity levels means the plan includes the Support API, and
   `SubscriptionRequiredException` means it doesn't. Change the plan in the
   console (Support Center → Support plans); see [AWS Support plans](https://aws.amazon.com/premiumsupport/plans/).
2. **Broadcom depot token** — from the [Broadcom Support Portal](https://support.broadcom.com) under your VCF entitlement, to enable VCF downloads.
   Use **Quick Links → Generate Download Token** and pick a Site ID with a VCF
   entitlement. If that option is missing, check that the VMware Cloud
   Foundation division is selected (drop-down next to your user name);
   otherwise ask your Broadcom User Administrator for the Product
   Administrator role ([KB 395778](https://knowledge.broadcom.com/external/article/395778),
   [KB 395322](https://knowledge.broadcom.com/external/article/395322)).
   Starting with VCF 9.1, the depot uses an activation code instead of a
   download token ([KB 443647](https://knowledge.broadcom.com/external/article/443647)).
3. **VCF SDDC Manager OVA** — download from the [Broadcom Support Portal](https://support.broadcom.com) under your VCF entitlement (e.g. `VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova`). Supported VCF versions: **9.0.2** and **9.1.0** — the OVA version must match `evs.vcf_version` in your blueprint.
4. **VMware ovftool** (Linux zip) — download from the [Broadcom Support Portal](https://support.broadcom.com) (e.g. `VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip`).
5. A machine with the **OVA and ovftool downloaded locally**, to upload them in
   step 4. If you follow the CLI commands rather than the console, you'll also
   need the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
   configured for the target account, plus `python3` (3.10+) with `boto3`
   and `pyyaml` for the quota check (step 3), the blueprint check (step 2),
   and teardown (`destroy.py` doesn't run on 3.9). Note that AWS CloudShell's
   home directory is too small for the OVA.

The setup steps below walk you through uploading these, creating
the secret, customizing the blueprint, and launching.

> **Shell examples:** CLI commands below are given for both shells. **Bash
> (Linux/macOS)** blocks target a POSIX shell; **PowerShell 7 (Windows)** blocks
> are tested on PowerShell 7+ (`pwsh`) and may not work on Windows PowerShell
> 5.1. Console (GUI) steps are OS-neutral and shown once.

## What to expect

1. **You create one secret** (your Broadcom depot token), **upload 3 files to
   your S3 bucket** (blueprint, OVA, ovftool), and **launch one CloudFormation
   stack** — that's the whole hands-on part.
2. **Your stack completes in ~5 minutes.** It only creates the runner instance
   and base networking — the VCF deployment itself hasn't started yet.
3. **A second stack appears** shortly after, named
   `<your-stack-name>-amazon-evs-<vcf-version>-infrastructure`. The runner
   creates it automatically; it holds the deployment infrastructure (NAT
   gateway, DNS zones, Route Server, security group). It completes within a
   few minutes. You never need to touch this stack — and don't delete it
   manually (teardown handles it).
4. **The deployment runs unattended for ~4-7 hours.** Bare-metal hosts are
   provisioned (~50 minutes into the process, with billing commencing at that time), VCF is installed and
   configured, and the NSX edge cluster is deployed.
5. **Monitor progress in CloudWatch Logs** — open the `OrchestratorLogsUrl`
   link in your **first** stack's Outputs tab. If you set `SnsTopicArn`,
   start/failure/success notifications are published to that topic — what you
   receive (email, SMS, etc.) depends on how you've subscribed to it.
6. **Done** only when the log shows `ALL STAGES COMPLETE` (and the success
   notification is published). See "After deployment" below for how to log in.
## Setup

> **Want to reach the vCenter / NSX / SDDC Manager UIs?** They're only reachable
> from inside the VPC. If you want easy access, set `jumpbox.enabled: true` in
> your blueprint (step 2 below) before launching — the deployment then includes
> a Windows jumpbox. See the [Windows jumpbox](#windows-jumpbox-optional) section
> for how to connect.

### 0. Get the code
The commands below (`check-quotas.py`, `destroy.py`) are run from your own
machine and expect to find those scripts in the current directory. Clone the
repo and `cd` into this tool's folder once, up front (identical in Bash and
PowerShell 7):
```bash
git clone https://github.com/aws/solutions-for-amazon-evs.git
cd solutions-for-amazon-evs/Deploy/EVS-Deployment-Orchestrator
```
Run every command below from this directory (`Deploy/EVS-Deployment-Orchestrator/`).

### 1. Create the depot token secret
One-time setup. Set your variables and run. The token is read from a hidden
prompt, so it isn't shown on screen or saved in your shell history:

**Bash (Linux/macOS):**
```bash
SECRET_NAME=evs-depot-token   # can be whatever you want — must match DepotSecretName at launch
REGION=us-east-2

read -rsp "Depot token: " DEPOT_TOKEN && echo
aws secretsmanager create-secret \
  --name $SECRET_NAME \
  --secret-string "$DEPOT_TOKEN" \
  --region $REGION
unset DEPOT_TOKEN
```

**PowerShell 7 (Windows):**
```powershell
$SECRET_NAME = "evs-depot-token"   # can be whatever you want — must match DepotSecretName at launch
$REGION = "us-east-2"

$DEPOT_TOKEN = Read-Host -MaskInput "Depot token"
aws secretsmanager create-secret `
  --name $SECRET_NAME `
  --secret-string "$DEPOT_TOKEN" `
  --region $REGION
Remove-Variable DEPOT_TOKEN
```
> The secret can have any name — just make sure the `DepotSecretName` stack
> parameter matches it when you launch (default: `evs-depot-token`). It must
> be in the same Region as the stack.
>
> To replace the value later (for example after a depot failure), run the
> same commands with `aws secretsmanager put-secret-value --secret-id $SECRET_NAME`
> in place of `create-secret --name $SECRET_NAME`.

### 2. Build your blueprint
Start from [`blueprints/custom.all-options.example.yaml`](blueprints/custom.all-options.example.yaml),
the full reference blueprint: every supported option is listed, commented out,
with notes inline. Copy it to `blueprint.yaml`, then uncomment and set the
fields you need.

**Bash (Linux/macOS):**
```bash
cp blueprints/custom.all-options.example.yaml blueprint.yaml
```

**PowerShell 7 (Windows):**
```powershell
Copy-Item blueprints/custom.all-options.example.yaml blueprint.yaml
```

At a minimum, uncomment and set every field marked `REQUIRED`:

- `dns.fqdn` — any private domain name you choose (e.g. `"vcf.mycompany.internal"`)
- `evs.environment_name` — display name for this environment (letters, digits, hyphens, underscores only)
- `evs.instance_type` — `i4i.metal` (128 vCPUs), `i7i.metal-24xl` (96 vCPUs), or `i7i.metal-48xl` (192 vCPUs), which use vSAN; or `r7i.metal-24xl` (96 vCPUs) or `r7i.metal-48xl` (192 vCPUs), which have no local disks and also need `storage.principal.type: fsxn` (see [Principal storage](#principal-storage-vsan-or-fsx-for-netapp-ontap))
- `evs.vcf_version` — `"9.0.2"`, `"9.1.0"` or `"9.1.1"`
- `evs.terms_accepted` and `evs.simple_deployment`
- `hostnames.esxi` (the host list), plus `hostnames.vcenter`, `hostnames.nsx`, and `hostnames.sddc_manager`
- all six `sizing.*` fields

These have no defaults — omitting any one fails validation at launch. Everything
not marked `REQUIRED` is optional. Need more (or fewer) hosts? The
`hostnames.esxi` list has inline guidance on changing the host count.

We recommend also uncommenting `requires`, which names the orchestrator
releases the blueprint works with:

```yaml
requires:
  edo: ">=1.0.0, <2.0.0"
```

At launch the orchestrator compares its own version with this range and stops,
before deploying anything, if the blueprint was written for a different
release. The message names the release you need. A blueprint without
`requires` is treated as written for v1.0.0 (`">=1.0.0, <2.0.0"`), so it
runs on every 1.x release. The orchestrator also warns about
top-level sections it doesn't use, such as a typo or a section from a newer
release.

#### Choosing an instance type and host count

Supported types change over time: check the Amazon EVS User Guide page
[VCF versions and EC2 instance types](https://docs.aws.amazon.com/evs/latest/userguide/versions-provided.html)
and run `aws evs get-versions --region $REGION`. All hosts in a cluster use
the same type. Published specs (from the
[EC2 storage optimized specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/so.html),
read 2026-10-16):

| Spec | `i4i.metal` | `i7i.metal-24xl` | `i7i.metal-48xl` |
|---|---|---|---|
| vCPU | 128 | 96 | 192 |
| Physical cores | 64 | 48 | 96 |
| Memory (GiB) | 1,024 | 768 | 1,536 |
| Instance store (raw vSAN capacity) | 8 x 3,750 GB NVMe | 6 x 3,750 GB NVMe | 12 x 3,750 GB NVMe |
| Cluster EVC mode | not set | `INTEL_SAPPHIRERAPIDS` | `INTEL_SAPPHIRERAPIDS` |

What the choice affects:

- **Host minimum:** with `simple_deployment: true`, 3 hosts for vSAN and 2 for
  FSx for NetApp ONTAP principal storage (diskless types); 4 with `false`
  (HA). Broadcom's VCF and vSAN requirements can call for more.
- **EC2 vCPU quota** (`L-1216C47A`) must be at least hosts × vCPUs per host.
  Step 3 checks it.
- **VCF subscription cores** must cover hosts × physical cores per host
  (User Guide, [VCF subscriptions](https://docs.aws.amazon.com/evs/latest/userguide/vcf-license-mgmt.html)).
- **Usable vSAN capacity** is lower than the raw instance store: the storage
  policy, host count, rebuild reserve, and the VCF management appliances all
  take a share. Amazon FSx for NetApp ONTAP can add external datastores.
- **Cost** scales with host count, type, and hours running; compare on the
  [Amazon EVS pricing page](https://aws.amazon.com/evs/pricing/).

This tool doesn't size a workload. For a real estate, confirm the host type
and count with AWS or an AWS Partner.

Not every type is offered in every Availability Zone. To list the AZs that
offer one:

**Bash (Linux/macOS):**
```bash
REGION=us-east-2
INSTANCE_TYPE=i4i.metal

aws ec2 describe-instance-type-offerings --region $REGION \
  --location-type availability-zone \
  --filters Name=instance-type,Values=$INSTANCE_TYPE \
  --query 'InstanceTypeOfferings[].Location' --output text
```

**PowerShell 7 (Windows):**
```powershell
$REGION = "us-east-2"
$INSTANCE_TYPE = "i4i.metal"

aws ec2 describe-instance-type-offerings --region $REGION `
  --location-type availability-zone `
  --filters Name=instance-type,Values=$INSTANCE_TYPE `
  --query 'InstanceTypeOfferings[].Location' --output text
```

An offered type can still be short of capacity when the hosts are created
(`InsufficientInstanceCapacity`).

#### Choosing the ESXi build

If you leave `evs.esx_version` out, the orchestrator picks the newest ESXi
build for the same VCF release (major.minor.patch) as `vcf_version`. For
`vcf_version: "9.1.0"` it picks an ESXi 9.1.0 build even when EVS also offers
9.1.1, because the VCF Installer rejects hosts from a different release. If EVS
offers no build for that release, the deployment stops when it creates the
hosts and lists the available builds. To see the build it will pick:

**Bash (Linux/macOS):**
```bash
REGION=us-east-2
INSTANCE_TYPE=i4i.metal
VCF_VERSION=9.1.0

aws evs get-versions --region $REGION \
  --query "instanceTypeEsxVersions[?instanceType=='$INSTANCE_TYPE'].esxVersions[]" --output text | tr '\t' '\n' \
  | grep "^ESXi-$VCF_VERSION\." | sort -t. -k2,2n -k3,3n -k4,4n -k5,5n | tail -1
```

**PowerShell 7 (Windows):**
```powershell
$REGION = "us-east-2"
$INSTANCE_TYPE = "i4i.metal"
$VCF_VERSION = "9.1.0"

(aws evs get-versions --region $REGION `
  --query "instanceTypeEsxVersions[?instanceType=='$INSTANCE_TYPE'].esxVersions[]" --output text) -split '\s+' |
  Where-Object { $_.StartsWith("ESXi-$VCF_VERSION.") } |
  Sort-Object { [long]$_.Split('.')[1] }, { [long]$_.Split('.')[2] }, { [long]$_.Split('.')[3] }, { [long]$_.Split('.')[4] } |
  Select-Object -Last 1
```

If the output is empty, EVS offers no build for that release on this instance
type in this Region; pick another type, Region, or VCF version. To use a
specific build instead, set `evs.esx_version` to it.

#### Check the blueprint

Run the structure check from this folder:

```bash
python3 tools/validate_blueprints.py blueprint.yaml
```

It doesn't catch everything. These otherwise fail only after launch, some
of them hours in, so check them by hand:

- **Host count:** the `hostnames.esxi` list has at least 3 entries with
  `simple_deployment: true`, or 4 with `false`.
- **HA sizing:** with `simple_deployment: false`, `operations_appliance_size`
  is `medium`, `large`, or `xlarge`.
- **Environment name:** letters, digits, hyphens, and underscores, starting
  with a letter or digit, at most 100 characters. EVS enforces it at the
  `aws_config` stage, not at launch.
- **Name length:** every `<hostname>.<dns.fqdn>` is at most 62 characters,
  or `CreateEnvironment` fails with a `ValidationException`. The orchestrator
  also creates `vcf-auto-platform.<dns.fqdn>`, so keep `dns.fqdn` to 44
  characters or fewer.
- **ESXi build:** see [Choosing the ESXi build](#choosing-the-esxi-build).
- **`evs.terms_accepted`** is `true`.

### 3. Check your account has room (optional but recommended)
Now that you have a real blueprint, verify your account has the quota headroom
this deployment needs — bare-metal vCPUs, VPC/subnet/route table/security
group/NAT gateway limits, EVS environment and host-count limits, Route 53
hosted zones and Resolver endpoints, and Secrets Manager. Set your variables
and run:

**Bash (Linux/macOS):**
```bash
BLUEPRINT=blueprint.yaml
REGION=us-east-2
AZ=us-east-2a

pip3 install boto3   # skip if you already have it (preinstalled in AWS CloudShell)
python3 check-quotas.py --blueprint $BLUEPRINT --region $REGION --availability-zone $AZ
```

**PowerShell 7 (Windows):**
```powershell
$BLUEPRINT = "blueprint.yaml"
$REGION = "us-east-2"
$AZ = "us-east-2a"

python -m pip install boto3   # skip if you already have it
python check-quotas.py --blueprint $BLUEPRINT --region $REGION --availability-zone $AZ
```

It compares what your blueprint needs against your service quotas **and**
what's already in use, and prints exact remediation commands for any
shortfall (add `--byo-vpc` if deploying into an existing VPC, plus
`--existing-vpc-id` for fully accurate per-VPC results). `--availability-zone`
should match the AZ you'll pass at launch — it scopes the NAT-gateway check,
which is skipped otherwise. Read-only — it changes nothing in your account.

A quota increase is a request AWS reviews, and it can take time. To track
the requests you've made:

```bash
aws service-quotas list-requested-service-quota-change-history --region $REGION \
  --query 'RequestedQuotas[].[QuotaName,DesiredValue,Status]' --output table
```

#### Other checks before launch

All read-only. Each one catches a launch that would otherwise fail, or a
deployment that collides with something already in the account:

| Check | Command | Pass when |
|---|---|---|
| Instance type offered in your AZ | `aws ec2 describe-instance-type-offerings --region $REGION --location-type availability-zone --filters Name=location,Values=$AZ Name=instance-type,Values=$INSTANCE_TYPE` | One offering returned |
| VCF version and ESXi build offered | `aws evs get-versions --region $REGION` | Lists your instance type with a build for your release |
| Depot secret in this Region | `aws secretsmanager describe-secret --region $REGION --secret-id $SECRET_NAME --query Name` | Returns the name |
| No address overlap | `aws ec2 describe-vpcs --region $REGION --query 'Vpcs[].CidrBlock'` | No range overlaps `<CidrPrefix>0.0/16` (default `10.20.0.0/16`), and neither do networks outside the account you'll connect to (on-premises, VPN, peered VPCs) |
| Stack name unused | `aws cloudformation describe-stacks --region $REGION --stack-name $STACK_NAME` | `does not exist` error |
| Environment name unused | `aws evs list-environments --region $REGION` | No environment (other than `DELETED`) with your `evs.environment_name` |
| DNS domain unused | `aws route53 list-hosted-zones-by-name --dns-name <your dns.fqdn> --max-items 1` | No hosted zone with exactly that name |

### 4. Upload the blueprint, OVA, and ovftool to S3
`$OVA` and `$OVF` below must point to where you actually downloaded them —
either `cd` there first, or set the variables to the full path (e.g.
`OVA=~/Downloads/VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova`). `$BLUEPRINT`
is the `blueprint.yaml` you created in step 2, in your current directory.

Set the bucket and region first — run this whether or not you need to create a
bucket, since the upload commands below use both:

**Bash (Linux/macOS):**
```bash
BUCKET=my-evs-deployment-bucket   # an existing bucket, or a new globally-unique name
REGION=us-east-2
```

**PowerShell 7 (Windows):**
```powershell
$BUCKET = "my-evs-deployment-bucket"   # an existing bucket, or a new globally-unique name
$REGION = "us-east-2"
```
If you don't already have a bucket, create it now (bucket names must be
globally unique, so pick something distinctive):

**Bash (Linux/macOS):**
```bash
aws s3api create-bucket --bucket $BUCKET --region $REGION \
  --create-bucket-configuration LocationConstraint=$REGION
```

**PowerShell 7 (Windows):**
```powershell
aws s3api create-bucket --bucket $BUCKET --region $REGION `
  --create-bucket-configuration LocationConstraint=$REGION
```
In `us-east-1`, omit the `--create-bucket-configuration` argument — that region
rejects an explicit `LocationConstraint`. An existing bucket must be in the
same Region as the stack (`aws s3api get-bucket-location --bucket $BUCKET`
prints `None` for `us-east-1`).

Before uploading, check the two Broadcom files. A wrong file isn't caught at
launch: it fails hours later, during VCF bringup.

**Bash (Linux/macOS):**
```bash
OVA=VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova
OVF=VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip

# The OVA's version, from inside the file (the first X.Y.Z must equal evs.vcf_version)
tar -xOf "$OVA" "$(tar -tf "$OVA" | grep -m1 '\.ovf$')" | grep -oiE '<(Full)?Version>[^<]*'
# ovftool must be the Linux build: this lists an ovftool binary
unzip -l "$OVF" | grep -E '(^|/)ovftool$'
```

**PowerShell 7 (Windows):**
```powershell
$OVA = "VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova"
$OVF = "VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip"

# The OVA's version, from inside the file (the first X.Y.Z must equal evs.vcf_version)
$OVF_NAME = tar -tf $OVA | Where-Object { $_ -like '*.ovf' } | Select-Object -First 1
tar -xOf $OVA $OVF_NAME | Select-String -Pattern '<(Full)?Version>[^<]*' -AllMatches | ForEach-Object { $_.Matches.Value }
# ovftool must be the Linux build: this lists an ovftool binary
tar -tf $OVF | Where-Object { $_ -match '(^|/)ovftool$' }
```

The `.ovf` is the first entry in the OVA, so these read only the start of the
file. A Windows `.msi` ovftool is always rejected: the runner is Linux.

Then upload the three files. Use the OVA version matching the blueprint you
copied in step 2 (a 9.1.0 blueprint needs a 9.1.0 OVA):

**Bash (Linux/macOS):**
```bash
BLUEPRINT=blueprint.yaml
OVA=VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova
OVF=VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip

aws s3 cp $BLUEPRINT s3://$BUCKET/blueprint.yaml --region $REGION \
  && aws s3 cp $OVA s3://$BUCKET/ --region $REGION \
  && aws s3 cp $OVF s3://$BUCKET/ --region $REGION
```

**PowerShell 7 (Windows):**
```powershell
$BLUEPRINT = "blueprint.yaml"
$OVA = "VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova"
$OVF = "VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip"

aws s3 cp $BLUEPRINT s3://$BUCKET/blueprint.yaml --region $REGION `
  && aws s3 cp $OVA s3://$BUCKET/ --region $REGION `
  && aws s3 cp $OVF s3://$BUCKET/ --region $REGION
```
Console: S3 → Create bucket (skip if you already have one) → open it → Upload (repeat for all three files).

### 5. Launch the stack

Create a stack from `evs-deployment-orchestrator.yaml` in the CloudFormation console
(Stacks → Create stack → Upload a template file), or via CLI.

The template is larger than the 51,200 bytes CloudFormation accepts inline
(`--template-body`), so the CLI launches from a copy in your S3 bucket
(`--template-url`). The commands below upload it first. The console's
"Upload a template file" does this for you.

**All parameters:**

| Parameter | Default | Notes |
|-----------|---------|-------|
| `BlueprintKey` | (none) | **Required.** S3 URI of your customized blueprint |
| `OvaKey` | (none) | **Required.** S3 URI of the SDDC Manager OVA |
| `OvfKey` | (none) | **Required.** S3 URI of the ovftool zip |
| `DepotSecretName` | `evs-depot-token` | **Required.** Name of your Broadcom depot token secret (step 1) |
| `AvailabilityZone` | (none) | **Required.** Must offer your chosen instance type |
| `CidrPrefix` | `10.20.` | First two octets for VLAN addressing |
| `SnsTopicArn` | (none) | Progress notifications: start, per-stage, ~2-hourly during long stages, and success/failure — each tagged with the stack name. [About SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-getting-started.html) |
| `KeyPairName` | (auto-created) | Optional. An existing EC2 key pair, used for **both** SSH to the runner and RDP to the Windows jumpbox (if enabled). If omitted, the stack auto-creates one named `<stack-name>-runner-key` and stores its private key in SSM Parameter Store. **If you supply your own, keep the `.pem` — AWS never has the private key for a key pair you created, so it is not retrievable from SSM and you need it to decrypt the jumpbox's Windows password.** |
| `RunnerInstanceType` | `t3.large` | Size of the **runner** — a small EC2 instance the stack automatically launches **in your account** to drive the deployment. Defaults to `t3.large` (billed to your account); you don't manage it, and teardown removes it. |
| `ExistingVpcId` + 4 subnet/route-table params | (none) | BYO-VPC — see section below |

**CLI example:**

**Bash (Linux/macOS):**
```bash
STACK_NAME=my-evs-deployment
REGION=us-east-2
BUCKET=my-evs-deployment-bucket
OVA=VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova
OVF=VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip
AZ=us-east-2a
SECRET_NAME=evs-depot-token   # the depot-token secret you created above

aws s3 cp evs-deployment-orchestrator.yaml s3://$BUCKET/evs-deployment-orchestrator.yaml --region $REGION

aws cloudformation create-stack \
  --stack-name $STACK_NAME \
  --region $REGION \
  --template-url https://$BUCKET.s3.$REGION.amazonaws.com/evs-deployment-orchestrator.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=BlueprintKey,ParameterValue=s3://$BUCKET/blueprint.yaml \
    ParameterKey=OvaKey,ParameterValue=s3://$BUCKET/$OVA \
    ParameterKey=OvfKey,ParameterValue=s3://$BUCKET/$OVF \
    ParameterKey=AvailabilityZone,ParameterValue=$AZ \
    ParameterKey=DepotSecretName,ParameterValue=$SECRET_NAME
```

**PowerShell 7 (Windows):**
```powershell
$STACK_NAME = "my-evs-deployment"
$REGION = "us-east-2"
$BUCKET = "my-evs-deployment-bucket"
$OVA = "VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova"
$OVF = "VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip"
$AZ = "us-east-2a"
$SECRET_NAME = "evs-depot-token"   # the depot-token secret you created above

aws s3 cp evs-deployment-orchestrator.yaml s3://$BUCKET/evs-deployment-orchestrator.yaml --region $REGION

aws cloudformation create-stack `
  --stack-name $STACK_NAME `
  --region $REGION `
  --template-url "https://$BUCKET.s3.$REGION.amazonaws.com/evs-deployment-orchestrator.yaml" `
  --capabilities CAPABILITY_IAM `
  --parameters `
    ParameterKey=BlueprintKey,ParameterValue=s3://$BUCKET/blueprint.yaml `
    ParameterKey=OvaKey,ParameterValue=s3://$BUCKET/$OVA `
    ParameterKey=OvfKey,ParameterValue=s3://$BUCKET/$OVF `
    ParameterKey=AvailabilityZone,ParameterValue=$AZ `
    ParameterKey=DepotSecretName,ParameterValue=$SECRET_NAME
```

**Optional parameters** are added as extra `ParameterKey=...,ParameterValue=...`
entries on the same `--parameters` list. Two you may want:

**Bash (Linux/macOS):**
```bash
    # Get email/SMS progress notifications (see "Notifications" below):
    ParameterKey=SnsTopicArn,ParameterValue=arn:aws:sns:$REGION:<account-id>:my-topic \
    # Bring your own EC2 key pair instead of letting the stack create one:
    ParameterKey=KeyPairName,ParameterValue=my-existing-key \
```

**PowerShell 7 (Windows):**
```powershell
    # Get email/SMS progress notifications (see "Notifications" below):
    ParameterKey=SnsTopicArn,ParameterValue=arn:aws:sns:${REGION}:<account-id>:my-topic `
    # Bring your own EC2 key pair instead of letting the stack create one:
    ParameterKey=KeyPairName,ParameterValue=my-existing-key `
```

> **Bringing your own key pair?** `KeyPairName` must be an EC2 key pair that
> **already exists in this region**, and it is used for **both** SSH to the
> runner and RDP to the Windows jumpbox. **Keep the `.pem` file** — AWS does not
> store the private key for a key pair you created, so it is *not* in SSM
> Parameter Store, and you need it to decrypt the jumpbox's Windows
> Administrator password (see [Windows jumpbox](#windows-jumpbox-optional)).
> If you omit `KeyPairName`, the stack creates `<stack-name>-runner-key` for you
> and puts its private key in SSM, which is the easier path.

Right after launch, check that the stack holds the parameters you meant.
Hosts aren't created until about 50 minutes in, so a mistake caught now costs
little: delete the stack and launch again. Don't update the stack instead;
an update re-runs the bootstrap on a new runner.

```bash
aws cloudformation describe-stacks --region $REGION --stack-name $STACK_NAME \
  --query 'Stacks[0].Parameters[].[ParameterKey,ParameterValue]' --output text
```

## Deployment options

What each option changes, so you can decide before launch:

| Option | Where you set it | What it changes |
|---|---|---|
| New VPC or bring your own | No BYO parameters, or the five `Existing*` stack parameters ([BYO-VPC](#byo-vpc)) | A new VPC is created and removed with the stack. With BYO-VPC, teardown removes only what the stack created |
| VLAN addressing | `CidrPrefix` (default `10.20.`) | First two octets of the EVS VLAN subnets. Must not overlap any network you'll connect to |
| Jumpbox | `jumpbox.enabled`, `jumpbox.instance_type` | A Windows instance with no inbound access by default; bills while it runs. Without it you need your own path into the VPC |
| HCX | `hcx.enabled`, `hcx.public_cidr` | Deploys HCX for migration and network extension. **Opens the HCX VLAN's network ACL to inbound 443/TCP and 4500 (TCP and UDP) from `0.0.0.0/0`.** Without `public_cidr`, an Amazon /28 is provisioned through IPAM (2 per Region) |
| NTP | `phase3.ntp` (default `time.aws.com`) | NTP servers for every VCF appliance |
| CEIP | `phase3.ceip` | VMware telemetry opt-in |
| Notifications | `SnsTopicArn` | See [Notifications](#notifications) |
| Key pair | `KeyPairName`, or let the stack create one | Used for the runner and the jumpbox |
| Staged deployment | Run the orchestrator on the runner with `--stop-after validate_dns`, later `--resume` | Builds and checks the landing zone (NAT gateway, DNS, Route Server, security group) without the EVS environment or hosts. The landing zone bills while it waits |
| Install method | This tool, or a manual install with [`spec-generator/`](spec-generator/) | The spec generator writes a bringup spec for the VCF Installer UI. That file holds every password in plaintext: keep it out of version control and delete it after use |

What this tool doesn't do: size a workload, upgrade or patch VCF, add or
replace hosts after deployment (see [Adding or removing hosts](#adding-or-removing-hosts)
for doing it yourself), run HCX migrations, or use Dedicated Hosts or EC2
placement groups (the blueprint has no fields for them). Day-2 operations
are yours: see [Amazon EVS host maintenance](https://docs.aws.amazon.com/evs/latest/userguide/evs-host-maintenance.html).

## Monitoring & recovery

Once your stack is launched, here's how to watch it run.

### Watch the logs (the easy way — recommended)

1. Open your **first** stack in the CloudFormation console.
2. On the **Outputs** tab, click the **`OrchestratorLogsUrl`** link (or use the
   **Resources** tab and click the orchestrator **log group**).
3. That opens the orchestrator's CloudWatch log group, where every stage
   streams in real time. Watch until it shows `ALL STAGES COMPLETE`.

That's all you need for normal monitoring — the deployment runs unattended and
reports its full progress here. If you set `SnsTopicArn` at launch, you also get
start / stage / success / failure notifications.

### Watch from the CLI

The same log, from a terminal. Log groups are kept after their stack is
deleted, so look yours up by the stack rather than by name:

**Bash (Linux/macOS):**
```bash
STACK_NAME=my-evs-deployment
REGION=us-east-2

LOG_GROUP=$(aws cloudformation describe-stack-resource --region $REGION --stack-name $STACK_NAME \
  --logical-resource-id OrchestratorLogGroup --query StackResourceDetail.PhysicalResourceId --output text)

# Both stacks (bootstrap + infrastructure)
aws cloudformation list-stacks --region $REGION \
  --query "StackSummaries[?starts_with(StackName,'$STACK_NAME') && StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]" --output table
# One line per stage start, finish, or failure
aws logs tail "$LOG_GROUP" --region $REGION --since 2d --format short --filter-pattern '?START ?DONE ?FAIL ?STAGES'
# Errors in the last hour
aws logs tail "$LOG_GROUP" --region $REGION --since 1h --format short --filter-pattern '?ERROR ?FAIL ?403'
# Live log (Ctrl+C to stop)
aws logs tail "$LOG_GROUP" --region $REGION --follow
# EVS environments and their state
aws evs list-environments --region $REGION \
  --query 'environmentSummaries[].[environmentId,environmentName,environmentState]' --output table
```

**PowerShell 7 (Windows):**
```powershell
$STACK_NAME = "my-evs-deployment"
$REGION = "us-east-2"

$LOG_GROUP = aws cloudformation describe-stack-resource --region $REGION --stack-name $STACK_NAME `
  --logical-resource-id OrchestratorLogGroup --query StackResourceDetail.PhysicalResourceId --output text

# Both stacks (bootstrap + infrastructure)
aws cloudformation list-stacks --region $REGION `
  --query "StackSummaries[?starts_with(StackName,'$STACK_NAME') && StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]" --output table
# One line per stage start, finish, or failure
aws logs tail $LOG_GROUP --region $REGION --since 2d --format short --filter-pattern '?START ?DONE ?FAIL ?STAGES'
# Errors in the last hour
aws logs tail $LOG_GROUP --region $REGION --since 1h --format short --filter-pattern '?ERROR ?FAIL ?403'
# Live log (Ctrl+C to stop)
aws logs tail $LOG_GROUP --region $REGION --follow
# EVS environments and their state
aws evs list-environments --region $REGION `
  --query 'environmentSummaries[].[environmentId,environmentName,environmentState]' --output table
```

For the hosts, add `aws evs list-environment-hosts --region $REGION --environment-id <environmentId>`
with the ID from the last command.

### Stage order and timing

| Stage | Typical duration | What you'll see |
|---|---|---|
| `aws_config`, `generate_config`, `validate_dns` | Minutes | The infrastructure stack reaches `CREATE_COMPLETE` |
| `phase2_deploy` | 45–100 min | The EVS environment and hosts are created (hosts bill from here) |
| `esxi_vlan_tag`, `esxi_vmfs_create`, `esxi_ova_deploy`, `esxi_ova_verify` | Minutes | The VCF Installer is deployed on the first host |
| `phase3_deploy` | 3–5.5 hours | Steps `deploy-vcf-and-edge N/6`: depot, downloads, bringup, NSX edge |

The first step of `phase3_deploy` configures the Broadcom depot and downloads
the binaries, so depot credential problems show up there. A depot that
reports `DEPOT_CONNECTION_SUCCESSFUL` and `SYNCED` can still fail later with
HTTP 403 on downloads if the credential isn't entitled for that version
([KB 440278](https://knowledge.broadcom.com/external/article/440278)).

### Telling "failed" apart from "still working"

A normal deployment takes 4–7 hours and individual stages are legitimately slow
— bare-metal host creation is typically 45–100 minutes, and VCF bringup plus the
NSX edge cluster 3–5.5 hours — so long gaps with no output are expected. What to
look for instead:

| Signal | Meaning |
|--------|---------|
| `ALL STAGES COMPLETE` in the log | Success |
| A line starting `FAIL  [<stage-id>]` | That stage failed; the log gives the reason |
| `MonitorCommand` tag reads `RUNNING` | Still working normally |
| Stack is `CREATE_FAILED` | Never started — check the `BootstrapLogsUrl` output instead of the orchestrator log group |

To resume, use the stack's **`ResumeCommand`** output — it already has the right
paths and flags for your stack. It continues from the first incomplete stage;
completed stages are checkpointed and skipped. To redo a stage that's already
checkpointed, swap `--resume` for `--start-from <stage-id>` (`--help` lists the
stage IDs).

### Get onto the runner yourself (optional)

Only needed if you want to intervene — for example, to resume a failed stage.
The runner is the small EC2 instance the stack created to do the work. You can
SSH in (requires a one-time security group update):

**Bash (Linux/macOS):**
```bash
KEY_FILE=<key-file>
RUNNER_PUBLIC_IP=<runner-public-ip>

ssh -i $KEY_FILE ec2-user@$RUNNER_PUBLIC_IP
```

**PowerShell 7 (Windows):**
```powershell
$KEY_FILE = "<key-file>"
$RUNNER_PUBLIC_IP = "<runner-public-ip>"

ssh -i $KEY_FILE ec2-user@$RUNNER_PUBLIC_IP
```

See "SSH access setup" below for how to retrieve the auto-created key and open
port 22. Once connected, the orchestrator code lives at:

```
/opt/evs/src/Deploy/EVS-Deployment-Orchestrator/orchestrator/
```

From that directory you can tail the live log, resume a failed stage, or run a
destroy. The stack's `ResumeCommand` and `DestroyCommand` outputs give you the
exact commands pre-filled.

### Quick reference

| Action | How |
|--------|-----|
| View logs | Open `OrchestratorLogsUrl` from your first stack's **Outputs** tab (the CloudWatch log group) — the primary way to monitor |
| Check status | `aws ec2 describe-tags` on the runner (see `MonitorCommand` output) |
| On the runner | `tail -f /opt/evs/orchestrator.log` |
| Resume after failure | Connect to the runner, `cd /opt/evs/src/Deploy/EVS-Deployment-Orchestrator/orchestrator`, run the orchestrator with `--resume` (see `ResumeCommand` output) |
| Diagnose a failure | See [Troubleshooting](#troubleshooting) below |
| Tear down | See "Teardown" section below |

## Troubleshooting

### Find where it stopped

1. **Bootstrap stack is `CREATE_FAILED`:** the run never started. Read the
   stack's events and the `BootstrapLogsUrl` log.
2. **Otherwise,** find the last `FAIL  [<stage-id>]` line in the orchestrator
   log ([Watch from the CLI](#watch-from-the-cli)). The lines after it give
   the reason; the "errors in the last hour" command shows more.
3. **Check what's still billing** while you decide: the EVS hosts
   (`aws evs list-environment-hosts`), the runner, the NAT gateway, and the
   jumpbox.
4. **Fix the cause, then resume or tear down.** If the same stage fails twice
   after a resume, contact AWS Support.

### Common failures

| Symptom | Likely cause and fix |
|---|---|
| `Unable to load paramfile file://...` | You're not in this folder. `cd` here, or use an absolute `file:///` path |
| Stack fails: no Business or Enterprise Support | Add a support plan, then launch again |
| Bootstrap stack fails at launch: artifact 403 or 404 (S3) | Wrong S3 URI, the bucket is in another Region, or a bucket policy blocks the runner role |
| Stack fails: instance type not offered in the AZ | Launch again in an AZ that offers it ([Choosing an instance type and host count](#choosing-an-instance-type-and-host-count)) |
| Stack fails: depot secret not found | The secret name differs from `DepotSecretName`, or the secret is in another Region |
| Stack fails: `Blueprint validation / preflight failed` | Read the message in `BootstrapLogsUrl`, fix `blueprint.yaml`, upload it again, and launch again |
| During `phase3_deploy`: depot configure or sync fails, or HTTP 403 on downloads | The Broadcom credential is invalid, expired, not entitled for this version ([KB 440278](https://knowledge.broadcom.com/external/article/440278)), or the wrong type for this release: starting with VCF 9.1 the depot uses an activation code instead of a download token ([KB 443647](https://knowledge.broadcom.com/external/article/443647)). Update the secret value (step 1), then resume |
| Invalid environment name at the `aws_config` stage | `evs.environment_name` breaks the EVS naming rule. Fix the blueprint and resume, or launch again |
| `CreateEnvironment` fails with `ValidationException` | A host name plus `dns.fqdn` is over 62 characters ([Check the blueprint](#check-the-blueprint)) |
| Bringup validation fails: `ESXi Host ... version X is not compatible with ... VCF Operations Y` | The hosts' ESXi build is from a different release than the installer OVA. The build of existing hosts can't be changed: tear down, set `evs.esx_version` ([Choosing the ESXi build](#choosing-the-esxi-build)), and launch again |
| `InsufficientInstanceCapacity` while creating hosts | The AZ is short of bare-metal capacity for that type. An immediate resume won't clear it, and each attempt re-provisions for about 45 minutes. Tear down and use another AZ or type, or arrange an On-Demand Capacity Reservation first |
| Quota shortfall | Request the increase `check-quotas.py` prints, wait for approval, then launch or resume |

### Before you resume

1. Fix the root cause first, and re-run the checks it affects (for example
   `check-quotas.py` after a quota change).
2. Connect to the runner ([SSH access setup](#ssh-access-setup-runner-only)).
3. Make sure no orchestrator is still running there: `pgrep -f deploy_orchestrator.py`
   must print nothing. Two runs at once corrupt the checkpoint.
4. Run the stack's `ResumeCommand` output as it is.

Don't update the bootstrap stack, replace the runner, or launch a second
stack to "retry": the checkpoints live on the runner, and a stack update
re-runs the bootstrap.

### Orchestrator flags

Run on the runner, from the directory in the `ResumeCommand` output.
`--help` lists the stage IDs.

| Flag | What it does |
|---|---|
| `--resume` | Continues from the first incomplete stage; completed stages are checkpointed and skipped |
| `--start-from <stage-id>` | Runs again from that stage, including stages already checkpointed |
| `--stop-after <stage-id>` | Runs up to and including that stage, then stops. `--stop-after validate_dns` builds only the landing zone |
| `--skip-prework` | Skips the four `esxi_*` stages. Use it only when they already finished, for example to retry from `phase3_deploy` |
| `--dry-run` | Validates the config and shows what would run, without running it |
| `--destroy` | Deletes the EVS environment from the runner: hosts, EBS volume, secrets, and environment. Irreversible; prefer [`destroy.py`](#teardown), which can also remove the infrastructure |

## After deployment

When the deployment succeeds (tag reads `SUCCEEDED`, log shows `ALL STAGES COMPLETE`):

- **vCenter**: `https://vc.<your-fqdn>` (e.g. `https://vc.vcf.mycompany.internal`)
- **NSX Manager**: `https://nsx.<your-fqdn>`
- **SDDC Manager**: `https://sddcm.<your-fqdn>`

These are on a private network inside your VPC. To reach them you need a
network path from your workstation — the easiest option is the optional
[Windows jumpbox](#windows-jumpbox-optional); VPN or AWS Client VPN also work.

**Credentials** are stored in AWS Secrets Manager, named
`evs-<environment-id>_<role>` (e.g., `evs-env-abc123_vcenterSso` for the
vCenter SSO administrator password). Retrieve them via CLI:

**Bash (Linux/macOS):**
```bash
REGION=us-east-2
ENVIRONMENT_ID=env-abc123

# List all secrets for your environment
aws secretsmanager list-secrets --region $REGION \
  --filters Key=name,Values=evs-$ENVIRONMENT_ID \
  --query 'SecretList[].Name' --output table

# Get a specific password (e.g. vCenter SSO admin)
aws secretsmanager get-secret-value --region $REGION \
  --secret-id evs-${ENVIRONMENT_ID}_vcenterSso \
  --query 'SecretString' --output text
```

**PowerShell 7 (Windows):**
```powershell
$REGION = "us-east-2"
$ENVIRONMENT_ID = "env-abc123"

# List all secrets for your environment
aws secretsmanager list-secrets --region $REGION `
  --filters Key=name,Values=evs-$ENVIRONMENT_ID `
  --query 'SecretList[].Name' --output table

# Get a specific password (e.g. vCenter SSO admin)
aws secretsmanager get-secret-value --region $REGION `
  --secret-id "evs-${ENVIRONMENT_ID}_vcenterSso" `
  --query 'SecretString' --output text
```

Common roles: `vcenterSso` (vCenter admin), `vcenterRoot`, `nsxAdmin`,
`nsxRoot`, `sddcManagerRoot`, `operationsAdmin`.

## Windows jumpbox (optional)

The vCenter/NSX/SDDC Manager UIs are only reachable from inside the VPC. The
easiest way in is the optional Windows jumpbox — set this in your blueprint
before launching:

```yaml
jumpbox:
  enabled: true
```

The jumpbox is created in the VPC's public subnet with a public IP but
**no inbound access** — you must open RDP to your own IP first. It uses the
**same key pair as the runner**, which determines how you get the Windows
Administrator password:

| At launch | Key pair used | Get the password with |
|---|---|---|
| You set `KeyPairName` | your key | **your own `.pem`** — AWS has no copy, so it is *not* in SSM |
| You omitted `KeyPairName` | `<stack-name>-runner-key` (auto-created) | the private key from SSM Parameter Store (commands below) |

**Bash (Linux/macOS):**
```bash
REGION=<region>
STACK=<your-stack-name>

# 1. Find the jumpbox (it's in the infrastructure stack's outputs)
INFRA=$(aws cloudformation list-stacks --region $REGION \
  --query "StackSummaries[?starts_with(StackName,'$STACK-amazon-evs-') && StackStatus=='CREATE_COMPLETE'].StackName" \
  --output text | head -1)
JUMPBOX_IP=$(aws cloudformation describe-stacks --region $REGION --stack-name $INFRA \
  --query 'Stacks[0].Outputs[?OutputKey==`JumpboxPublicIp`].OutputValue' --output text)
JUMPBOX_SG=$(aws cloudformation describe-stacks --region $REGION --stack-name $INFRA \
  --query 'Stacks[0].Outputs[?OutputKey==`JumpboxSecurityGroupId`].OutputValue' --output text)
JUMPBOX_ID=$(aws cloudformation describe-stacks --region $REGION --stack-name $INFRA \
  --query 'Stacks[0].Outputs[?OutputKey==`JumpboxInstanceId`].OutputValue' --output text)

# 2. Allow RDP from your current IP only
MY_IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --region $REGION \
  --group-id $JUMPBOX_SG --protocol tcp --port 3389 --cidr $MY_IP/32

# 3. Get the Windows Administrator password
# The jumpbox uses the SAME key pair as the runner.
# If you set KeyPairName at launch,
# use YOUR OWN .pem file for that key pair - AWS never has its private key,
# so it isn't retrievable from SSM.
#   aws ec2 get-password-data --region $REGION --instance-id $JUMPBOX_ID \
#     --priv-launch-key /path/to/your-key.pem --query PasswordData --output text
# Otherwise (no KeyPairName set, auto-created key), retrieve it from SSM:
KEY_ID=$(aws ec2 describe-key-pairs --region $REGION \
  --key-names $STACK-runner-key --query 'KeyPairs[0].KeyPairId' --output text)
aws ssm get-parameter --region $REGION --name /ec2/keypair/$KEY_ID \
  --with-decryption --query Parameter.Value --output text > jumpbox-key.pem
chmod 400 jumpbox-key.pem
aws ec2 get-password-data --region $REGION --instance-id $JUMPBOX_ID \
  --priv-launch-key jumpbox-key.pem --query PasswordData --output text
```

**PowerShell 7 (Windows):**
```powershell
$REGION = "<region>"
$STACK = "<your-stack-name>"

# 1. Find the jumpbox (it's in the infrastructure stack's outputs)
$INFRA = aws cloudformation list-stacks --region $REGION `
  --query "StackSummaries[?starts_with(StackName,'$STACK-amazon-evs-') && StackStatus=='CREATE_COMPLETE'].StackName" `
  --output text | Select-Object -First 1
$JUMPBOX_IP = aws cloudformation describe-stacks --region $REGION --stack-name $INFRA `
  --query 'Stacks[0].Outputs[?OutputKey==`JumpboxPublicIp`].OutputValue' --output text
$JUMPBOX_SG = aws cloudformation describe-stacks --region $REGION --stack-name $INFRA `
  --query 'Stacks[0].Outputs[?OutputKey==`JumpboxSecurityGroupId`].OutputValue' --output text
$JUMPBOX_ID = aws cloudformation describe-stacks --region $REGION --stack-name $INFRA `
  --query 'Stacks[0].Outputs[?OutputKey==`JumpboxInstanceId`].OutputValue' --output text

# 2. Allow RDP from your current IP only
$MY_IP = Invoke-RestMethod https://checkip.amazonaws.com
aws ec2 authorize-security-group-ingress --region $REGION `
  --group-id $JUMPBOX_SG --protocol tcp --port 3389 --cidr "$($MY_IP.Trim())/32"

# 3. Get the Windows Administrator password
# The jumpbox uses the SAME key pair as the runner.
# If you set KeyPairName at launch,
# use YOUR OWN .pem file for that key pair - AWS never has its private key,
# so it isn't retrievable from SSM.
#   aws ec2 get-password-data --region $REGION --instance-id $JUMPBOX_ID `
#     --priv-launch-key /path/to/your-key.pem --query PasswordData --output text
# Otherwise (no KeyPairName set, auto-created key), retrieve it from SSM:
$KEY_ID = aws ec2 describe-key-pairs --region $REGION `
  --key-names "$STACK-runner-key" --query 'KeyPairs[0].KeyPairId' --output text
aws ssm get-parameter --region $REGION --name /ec2/keypair/$KEY_ID `
  --with-decryption --query Parameter.Value --output text | Set-Content -Encoding ascii jumpbox-key.pem
aws ec2 get-password-data --region $REGION --instance-id $JUMPBOX_ID `
  --priv-launch-key jumpbox-key.pem --query PasswordData --output text
```

Console path: EC2 → Instances → `evs-jumpbox` → Security tab → open its
security group → Edit inbound rules → Add rule: RDP, source "My IP". Then
Instance → Connect → RDP client → Get password (upload the key from SSM
Parameter Store `/ec2/keypair/<key-id>`).

RDP to `$JUMPBOX_IP` as `Administrator`, open a browser, and the
`https://vc.<your-fqdn>` links from the success email work directly — DNS and
routing inside the VPC are already wired up. Treat the jumpbox as an admin
workstation: it can reach every VCF management appliance.

### Other ways to allow RDP

These use the Bash variables from the steps above.

- **A managed prefix list** your organization already keeps for its
  corporate or VPN ranges, instead of your current IP. List them with
  `aws ec2 describe-managed-prefix-lists --region $REGION --query "PrefixLists[].[PrefixListId,PrefixListName]" --output text`,
  then add the rule with the prefix list as its source:

  ```bash
  PL_ID=pl-0123456789abcdef0   # the prefix list you chose
  aws ec2 authorize-security-group-ingress --region $REGION --group-id $JUMPBOX_SG \
    --ip-permissions "IpProtocol=tcp,FromPort=3389,ToPort=3389,PrefixListIds=[{Description=EVS jumpbox RDP,PrefixListId=$PL_ID}]"
  ```

- **No inbound rule at all:** Session Manager port forwarding. It needs an
  instance profile with the `AmazonSSMManagedInstanceCore` policy on the
  jumpbox, and the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  installed locally. Then RDP to `localhost:13389`; access is
  IAM-authenticated and logged in CloudTrail:

  ```bash
  aws ssm start-session --region $REGION --target $JUMPBOX_ID \
    --document-name AWS-StartPortForwardingSession \
    --parameters portNumber=3389,localPortNumber=13389
  ```

Never open RDP to `0.0.0.0/0`. When you're done, remove the rule with
`aws ec2 revoke-security-group-ingress` and exactly the same arguments you
added it with.

## Adding or removing hosts

The orchestrator builds the environment once; after `ALL STAGES COMPLETE`,
you add or remove hosts yourself. EVS handles the EC2 side, and you do the
VCF side in SDDC Manager (User Guide,
[Create an Amazon EVS host](https://docs.aws.amazon.com/evs/latest/userguide/evs-env-create-host.html)
and [Delete an Amazon EVS host](https://docs.aws.amazon.com/evs/latest/userguide/evs-env-delete-host.html)).

> Commands in this section are Bash, using `REGION` and `STACK_NAME` from
> earlier steps. In PowerShell 7, use `$VAR = ...` assignments and backtick
> line continuations.

### Add a host

1. **Check quotas.** Copy `blueprint.yaml`, add one entry to its
   `hostnames.esxi` list, and run `check-quotas.py` on the copy. Host
   creation fails if the hosts-per-environment quota is too low.
2. **Use the cluster's ESXi build** (`evs.esx_version` from your blueprint,
   or the build the existing hosts run). A host with a newer build than the
   cluster image can't be downgraded; it has to be deleted and recreated.
3. **Create the DNS records first.** Each host needs an A record in the
   forward zone and a PTR record in the reverse zone. The infrastructure
   stack created both zones (logical IDs `ForwardZone` and `ReverseZone`).
   Pick the next name and address after the existing hosts (for example
   `esxi04` at the next free address in the same subnet), keeping
   `<name>.<fqdn>` at 62 characters or fewer:

   ```bash
   INFRA=$(aws cloudformation list-stacks --region $REGION \
     --query "StackSummaries[?starts_with(StackName,'$STACK_NAME-amazon-evs-') && StackStatus!='DELETE_COMPLETE'].StackName" --output text)
   FWD_ZONE=$(aws cloudformation describe-stack-resource --region $REGION --stack-name "$INFRA" \
     --logical-resource-id ForwardZone --query StackResourceDetail.PhysicalResourceId --output text)
   REV_ZONE=$(aws cloudformation describe-stack-resource --region $REGION --stack-name "$INFRA" \
     --logical-resource-id ReverseZone --query StackResourceDetail.PhysicalResourceId --output text)
   KEY_NAME=$(aws cloudformation describe-stacks --region $REGION --stack-name "$INFRA" \
     --query "Stacks[0].Outputs[?OutputKey=='KeyName'].OutputValue" --output text)
   # The existing hosts' names and addresses
   aws route53 list-resource-record-sets --hosted-zone-id "$FWD_ZONE" \
     --query "ResourceRecordSets[?Type=='A'].[Name,ResourceRecords[0].Value]" --output table
   ```

   Then create the two records, in the Route 53 console or with
   `aws route53 change-resource-record-sets`: an `A` record
   `esxi04.<fqdn>` → the new address in the forward zone, and a `PTR` record
   for the reversed address (for example `5.0.20.10.in-addr.arpa` for
   `10.20.0.5`) → `esxi04.<fqdn>` in the reverse zone.
4. **Create the host** (it bills from creation):

   ```bash
   ENVIRONMENT_ID=env-abc123
   ESX_VERSION=ESXi-9.1.0.0100.25433460   # the cluster's build
   INSTANCE_TYPE=i4i.metal                # the cluster's instance type
   aws evs create-environment-host --region $REGION --environment-id $ENVIRONMENT_ID \
     --esx-version $ESX_VERSION \
     --host "{\"hostName\":\"esxi04\",\"keyName\":\"$KEY_NAME\",\"instanceType\":\"$INSTANCE_TYPE\"}"
   aws evs list-environment-hosts --region $REGION --environment-id $ENVIRONMENT_ID \
     --query "environmentHosts[].[hostName,hostState,stateDetails]" --output text
   ```

5. **When it's `CREATED`,** commission it in SDDC Manager and add it to the
   cluster (Broadcom VCF documentation, "Commission Hosts").

### Remove a host

1. **Decommission it in SDDC Manager first.** Deleting a host that wasn't
   decommissioned leaves stale data in vCenter and SDDC Manager.
2. **Keep the minimum:** with `simple_deployment: true`, 3 hosts for vSAN or 2
   for FSx for NetApp ONTAP principal storage; 4 with `false`; or Broadcom's
   minimum for your topology if higher.
3. **Delete it through the EVS API or console, never EC2.** Stopping or
   terminating the EC2 instance directly can cause data loss:

   ```bash
   aws evs delete-environment-host --region $REGION --environment-id $ENVIRONMENT_ID --host-name esxi04
   ```

4. **Remove its A and PTR records** once it's `DELETED`.

Before teardown, delete every DNS record you added. Route 53 deletes a hosted
zone only when it holds just its NS and SOA records, so an extra record
blocks deleting the infrastructure stack.

## File layout

```
├── README.md                  ← you are here
├── evs-deployment-orchestrator.yaml         ← the CloudFormation template (launch this)
├── blueprints/                ← the reference blueprint (copy it, customize it)
│   └── custom.all-options.example.yaml   ← every option, commented, with inline notes
├── check-quotas.py            ← pre-launch quota preflight (run this)
├── destroy.py                 ← standalone teardown script
├── tools/validate_blueprints.py   ← blueprint structure check (run this)
├── orchestrator/              ← the deployment automation (runs on the runner)
│   ├── deploy_orchestrator.py
│   ├── principal_storage.py   ← storage.principal.type: vsan or fsxn (FSx for NetApp ONTAP)
│   ├── evs_environment/       ← EVS environment + host creation
│   └── vcf_deployment/        ← VCF bringup, depot, edge, connector
└── spec-generator/            ← interactive blueprint/spec builder (optional helper)
```

## Principal storage: vSAN or FSx for NetApp ONTAP

The management cluster's datastore is set by `storage.principal` in the blueprint.
`storage` is optional: leaving it out is the same as `type: "vsan"`. For
`type: "fsxn"`, the FSx settings go in a block named after the type, `fsxn`.

| `storage.principal` | Datastore | Instance types |
|---|---|---|
| `type: "vsan"`, or no `storage` | vSAN ESA on the hosts' local NVMe | Types with local instance storage |
| `type: "fsxn"` without `fsxn.volume_id` | FSx for NetApp ONTAP the orchestrator creates | Types without local instance storage |
| `type: "fsxn"` with `fsxn.volume_id` | Your own FSx for NetApp ONTAP (BYO-FSxN) | Types without local instance storage |

At launch the orchestrator asks EC2 (`ec2:DescribeInstanceTypes`) whether the
instance type has local instance storage. Without it, vSAN cannot be built and
the blueprint must use FSx for NetApp ONTAP; with it, the type uses vSAN.

```yaml
storage:
  principal:
    type: "fsxn"
    name: "nfs-datastore"        # vSphere datastore name
    fsxn:
      file_system_name: "my-vcf-env-fsxn"
      storage_capacity_gb: 2048
      throughput_capacity_mbps: 768
      storage_efficiency: true
      s3_access: false
```

**Create mode** applies when `volume_id` is not set, and only when the stack
creates the VPC. It mirrors the FSx console's Quick create, and every field is
required: `file_system_name`, `storage_capacity_gb` (1024 to 307200 GiB), `throughput_capacity_mbps`
(384, 768, 1536, 3072 or 6144, or `"recommended"` = 384), `storage_efficiency` and `s3_access`
(`false` only for now). There are no network or deployment-type fields: the
file system is always Single-AZ. The file system, its SVM `fsx` and NFS
volume `/vol1` are created in the service access subnet before any bare metal,
in a separate `<stack-name>-evs-storage` stack, with a security group admitting
NFS only from the host management VLAN subnet (`<CidrPrefix>10.0/24`).
Capacity can only grow; throughput, storage efficiency and the name can change.
Until `phase2_deploy` has finished, change them in the blueprint and re-run
with `--resume`. An edit that would replace the file system is refused before
anything changes. After that, don't edit the blueprint: any edit makes the
next run start fresh and repeat every stage, including VCF bring-up, on the
live environment. Change the file system in the FSx console or with
`aws fsx update-file-system` (capacity, throughput), `aws fsx update-volume`
(storage efficiency, and volume size after a capacity increase) and
`aws fsx tag-resource` (name). CloudFormation then shows the
`<stack-name>-evs-storage` stack as drifted. Automatic FSx backups are off, so plan your
own backups of the management appliances.

**Bring your own (BYO-FSxN)** needs [BYO-VPC](#byo-vpc): the file system must
already exist in that VPC, in the hosts' Availability Zone. Give only the
volume ID; the orchestrator reads the file system, SVM, junction path and NFS
endpoint from FSx. If creation fields are also set, the orchestrator uses the
volume, ignores them and logs a warning.

```yaml
storage:
  principal:
    type: "fsxn"
    name: "nfs-datastore"
    fsxn:
      volume_id: "fsvol-0123456789abcdef0"   # an ONTAP volume in this Region
```

Before you launch, the file system must be Single-AZ, the volume RW with UNIX
security style and a junction path, its export policy must allow the host
management VLAN subnet `<CidrPrefix>10.0/24` read-write with superuser `sys`,
and the file system's security group must allow TCP and UDP 111, 635, 2049 and
4045-4046 from that subnet. The orchestrator checks all of this except the export
policy before creating any host, and reports any missing security group rule.
It does not read the export policy: a read-only or root-squashing export fails
at the first NFS mount, after the hosts exist. The orchestrator never edits,
resizes or deletes your file system.

With either mode the orchestrator mounts the datastore on every host, takes
each host out of the one-node vSAN cluster EVS creates, and hands VCF bring-up
an NFS datastore instead of vSAN.

## BYO-VPC

Use an existing VPC instead of letting the stack create one. Start from
whichever step matches what you already have — skip anything already done.

### Step 1: VPC (skip if you already have one)

**Bash (Linux/macOS):**
```bash
REGION=us-east-2
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.30.0.0/16 --region $REGION \
  --query 'Vpc.VpcId' --output text)
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-support --region $REGION
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames --region $REGION
```

**PowerShell 7 (Windows):**
```powershell
$REGION = "us-east-2"
$VPC_ID = aws ec2 create-vpc --cidr-block 10.30.0.0/16 --region $REGION `
  --query 'Vpc.VpcId' --output text
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-support --region $REGION
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames --region $REGION
```
Console: VPC → Your VPCs → Create VPC → VPC only → CIDR `10.30.0.0/16`.
Enable DNS resolution and DNS hostnames under Actions → Edit VPC settings.

### Step 2: Internet gateway (skip if your VPC already has one)

**Bash (Linux/macOS):**
```bash
IGW_ID=$(aws ec2 create-internet-gateway --region $REGION \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --vpc-id $VPC_ID --internet-gateway-id $IGW_ID --region $REGION
```

**PowerShell 7 (Windows):**
```powershell
$IGW_ID = aws ec2 create-internet-gateway --region $REGION `
  --query 'InternetGateway.InternetGatewayId' --output text
aws ec2 attach-internet-gateway --vpc-id $VPC_ID --internet-gateway-id $IGW_ID --region $REGION
```
Console: VPC → Internet gateways → Create → then Actions → Attach to VPC.

### Step 3: Subnets (create any you don't already have)

You need 3 subnets, all in the **same AZ**:

**Bash (Linux/macOS):**
```bash
AZ=us-east-2a

# Public subnet (for the NAT gateway)
PUBLIC_SUBNET=$(aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.30.5.0/24 \
  --availability-zone $AZ --region $REGION --query 'Subnet.SubnetId' --output text)

# Service-access subnet (EVS uses this for its control-plane connection)
SERVICE_SUBNET=$(aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.30.0.0/24 \
  --availability-zone $AZ --region $REGION --query 'Subnet.SubnetId' --output text)

# Runner subnet (where the bootstrap runner instance launches)
RUNNER_SUBNET=$(aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.30.6.0/24 \
  --availability-zone $AZ --region $REGION --query 'Subnet.SubnetId' --output text)

# Enable public IPs on runner + public subnets
aws ec2 modify-subnet-attribute --subnet-id $PUBLIC_SUBNET --map-public-ip-on-launch --region $REGION
aws ec2 modify-subnet-attribute --subnet-id $RUNNER_SUBNET --map-public-ip-on-launch --region $REGION
```

**PowerShell 7 (Windows):**
```powershell
$AZ = "us-east-2a"

# Public subnet (for the NAT gateway)
$PUBLIC_SUBNET = aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.30.5.0/24 `
  --availability-zone $AZ --region $REGION --query 'Subnet.SubnetId' --output text

# Service-access subnet (EVS uses this for its control-plane connection)
$SERVICE_SUBNET = aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.30.0.0/24 `
  --availability-zone $AZ --region $REGION --query 'Subnet.SubnetId' --output text

# Runner subnet (where the bootstrap runner instance launches)
$RUNNER_SUBNET = aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.30.6.0/24 `
  --availability-zone $AZ --region $REGION --query 'Subnet.SubnetId' --output text

# Enable public IPs on runner + public subnets
aws ec2 modify-subnet-attribute --subnet-id $PUBLIC_SUBNET --map-public-ip-on-launch --region $REGION
aws ec2 modify-subnet-attribute --subnet-id $RUNNER_SUBNET --map-public-ip-on-launch --region $REGION
```
Console: VPC → Subnets → Create subnet (repeat 3x). Then select each of the
runner/public subnets → Actions → Edit subnet settings → enable auto-assign public IP.

### Step 4: Route tables

**Bash (Linux/macOS):**
```bash
# Public route table — gives runner + public subnets a path to the internet
PUBLIC_RT=$(aws ec2 create-route-table --vpc-id $VPC_ID --region $REGION \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $PUBLIC_RT --destination-cidr-block 0.0.0.0/0 \
  --gateway-id $IGW_ID --region $REGION
aws ec2 associate-route-table --route-table-id $PUBLIC_RT --subnet-id $PUBLIC_SUBNET --region $REGION
aws ec2 associate-route-table --route-table-id $PUBLIC_RT --subnet-id $RUNNER_SUBNET --region $REGION

# Service-access route table — leave empty, the orchestrator adds the NAT route later
SERVICE_RT=$(aws ec2 create-route-table --vpc-id $VPC_ID --region $REGION \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 associate-route-table --route-table-id $SERVICE_RT --subnet-id $SERVICE_SUBNET --region $REGION
```

**PowerShell 7 (Windows):**
```powershell
# Public route table — gives runner + public subnets a path to the internet
$PUBLIC_RT = aws ec2 create-route-table --vpc-id $VPC_ID --region $REGION `
  --query 'RouteTable.RouteTableId' --output text
aws ec2 create-route --route-table-id $PUBLIC_RT --destination-cidr-block 0.0.0.0/0 `
  --gateway-id $IGW_ID --region $REGION
aws ec2 associate-route-table --route-table-id $PUBLIC_RT --subnet-id $PUBLIC_SUBNET --region $REGION
aws ec2 associate-route-table --route-table-id $PUBLIC_RT --subnet-id $RUNNER_SUBNET --region $REGION

# Service-access route table — leave empty, the orchestrator adds the NAT route later
$SERVICE_RT = aws ec2 create-route-table --vpc-id $VPC_ID --region $REGION `
  --query 'RouteTable.RouteTableId' --output text
aws ec2 associate-route-table --route-table-id $SERVICE_RT --subnet-id $SERVICE_SUBNET --region $REGION
```
Console: VPC → Route tables → Create (2x). For the public one: Routes tab →
Edit → add `0.0.0.0/0` → target: your IGW. For both: Subnet associations →
Edit → check the appropriate subnets.

### Step 5: Use these values at stack launch

**Bash (Linux/macOS):**
```bash
echo "ExistingVpcId:                     $VPC_ID"
echo "ExistingRunnerSubnetId:            $RUNNER_SUBNET"
echo "ExistingServiceAccessSubnetId:     $SERVICE_SUBNET"
echo "ExistingServiceAccessRouteTableId: $SERVICE_RT"
echo "ExistingPublicSubnetId:            $PUBLIC_SUBNET"
```

**PowerShell 7 (Windows):**
```powershell
Write-Host "ExistingVpcId:                     $VPC_ID"
Write-Host "ExistingRunnerSubnetId:            $RUNNER_SUBNET"
Write-Host "ExistingServiceAccessSubnetId:     $SERVICE_SUBNET"
Write-Host "ExistingServiceAccessRouteTableId: $SERVICE_RT"
Write-Host "ExistingPublicSubnetId:            $PUBLIC_SUBNET"
```

Set `CidrPrefix` to match your VPC's range (e.g. `10.30.` for `10.30.0.0/16`).
This prefix is used for EVS VLAN subnets — a separate address space from the
subnets you just created.

## SSH access setup (runner only)

> This section is **only for SSH'ing into the Linux orchestrator runner** — the
> small EC2 instance that drives the deployment. You normally never need it;
> use it only to intervene (e.g. resume a failed stage). It is **not** how you
> reach the VCF UIs (vCenter/NSX/SDDC Manager) — those are private to the VPC,
> see [Windows jumpbox](#windows-jumpbox-optional). And it is **not** how you
> connect to the ESXi hosts, which you manage through vCenter.

If you supplied your own `KeyPairName` at launch, skip steps 1 and use that
key's `.pem` directly. Otherwise the stack auto-created a key pair for the
runner (named `<stack-name>-runner-key`) with its private key stored in SSM
Parameter Store. To SSH in:

1. **Get the key pair ID and retrieve the private key:**

   **Bash (Linux/macOS):**
   ```bash
   STACK_NAME=<stack-name>
   REGION=<region>

   # Find the key pair ID
   KEY_ID=$(aws ec2 describe-key-pairs \
     --key-names $STACK_NAME-runner-key \
     --region $REGION \
     --query 'KeyPairs[0].KeyPairId' --output text)

   # Download the private key
   aws ssm get-parameter \
     --name /ec2/keypair/$KEY_ID \
     --with-decryption \
     --region $REGION \
     --query 'Parameter.Value' --output text > runner-key.pem

   chmod 400 runner-key.pem
   ```

   **PowerShell 7 (Windows):**
   ```powershell
   $STACK_NAME = "<stack-name>"
   $REGION = "<region>"

   # Find the key pair ID
   $KEY_ID = aws ec2 describe-key-pairs `
     --key-names "$STACK_NAME-runner-key" `
     --region $REGION `
     --query 'KeyPairs[0].KeyPairId' --output text

   # Download the private key
   aws ssm get-parameter `
     --name /ec2/keypair/$KEY_ID `
     --with-decryption `
     --region $REGION `
     --query 'Parameter.Value' --output text | Set-Content -Encoding ascii runner-key.pem
   ```

2. **Add an inbound SSH rule to the runner's security group** (scoped to your IP):

   **Bash (Linux/macOS):**
   ```bash
   RUNNER_INSTANCE_ID=<runner-instance-id>
   REGION=<region>

   # Find the runner's security group
   SG_ID=$(aws ec2 describe-instances \
     --instance-ids $RUNNER_INSTANCE_ID \
     --region $REGION \
     --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)

   # Allow SSH from your current IP
   MY_IP=$(curl -s https://checkip.amazonaws.com)
   aws ec2 authorize-security-group-ingress \
     --group-id $SG_ID \
     --protocol tcp \
     --port 22 \
     --cidr $MY_IP/32 \
     --region $REGION
   ```

   **PowerShell 7 (Windows):**
   ```powershell
   $RUNNER_INSTANCE_ID = "<runner-instance-id>"
   $REGION = "<region>"

   # Find the runner's security group
   $SG_ID = aws ec2 describe-instances `
     --instance-ids $RUNNER_INSTANCE_ID `
     --region $REGION `
     --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text

   # Allow SSH from your current IP
   $MY_IP = Invoke-RestMethod https://checkip.amazonaws.com
   aws ec2 authorize-security-group-ingress `
     --group-id $SG_ID `
     --protocol tcp `
     --port 22 `
     --cidr "$($MY_IP.Trim())/32" `
     --region $REGION
   ```

3. **Connect:**

   **Bash (Linux/macOS):**
   ```bash
   RUNNER_PUBLIC_IP=<runner-public-ip>

   ssh -i runner-key.pem ec2-user@$RUNNER_PUBLIC_IP
   ```

   **PowerShell 7 (Windows):**
   ```powershell
   $RUNNER_PUBLIC_IP = "<runner-public-ip>"

   ssh -i runner-key.pem ec2-user@$RUNNER_PUBLIC_IP
   ```
   The runner's public IP is visible in the EC2 console or via:

   **Bash (Linux/macOS):**
   ```bash
   RUNNER_INSTANCE_ID=<runner-instance-id>
   REGION=<region>

   aws ec2 describe-instances --instance-ids $RUNNER_INSTANCE_ID \
     --region $REGION --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
   ```

   **PowerShell 7 (Windows):**
   ```powershell
   $RUNNER_INSTANCE_ID = "<runner-instance-id>"
   $REGION = "<region>"

   aws ec2 describe-instances --instance-ids $RUNNER_INSTANCE_ID `
     --region $REGION --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
   ```

## Teardown

> [!NOTE]
> Run `destroy.py` **before** deleting the CloudFormation stack. The ESXi hosts
> are created through the EVS API rather than by the stack, so deleting the
> stack leaves them running and billing — you'd then have to delete the
> environment and its hosts yourself in the EVS console. Destroying first does
> it for you.

Tear everything down with the standalone `destroy.py` script (from any machine with `boto3` + AWS credentials):

Teardown keeps the FSx for NetApp ONTAP storage stack
(`<stack-name>-evs-storage`) when the orchestrator created one: it holds the
datastore with the management appliances, it keeps billing, and after a
successful deployment it has CloudFormation termination protection. Add
`--delete-storage` to any level to delete it after the environment is gone,
with the datastore and every VM on it; teardown turns the protection off first.
`--all` needs `--delete-storage` when that stack exists, because the bootstrap
stack's subnet holds the file system. The runner's `--destroy` command takes
the same `--delete-storage` flag. A BYO-FSxN file system is never modified or
deleted.

Deleting the file system can take from about 20 minutes to well over 40, so
teardown waits up to 2 hours for it. Teardown deletes the volume itself without
a final backup (`SkipFinalBackup`) before it deletes the stack, so no FSx backup
is left billing. It never lists or deletes backups, so backups you made
yourself are kept. If teardown stops early (the volume or the stack is still
deleting, or a delete failed), it keeps the landing zone and bootstrap stacks
and says so; re-run the same command.

**Bash (Linux/macOS):**
```bash
STACK_NAME=<stack-name>
REGION=<region>

# Environment only (safest — leaves infra for reuse/redeployment)
python3 destroy.py --bootstrap-stack $STACK_NAME --region $REGION

# Also remove landing zone (NAT, Route Server, SG, key pair)
python3 destroy.py --bootstrap-stack $STACK_NAME --region $REGION --include-infra

# Full nuke — removes everything including VPC, runner, and the bootstrap stack
# In BYO-VPC mode, only resources created by this stack inside the VPC
# will be removed — nothing pre-existing is touched.
python3 destroy.py --bootstrap-stack $STACK_NAME --region $REGION --all
```

**PowerShell 7 (Windows):**
```powershell
$STACK_NAME = "<stack-name>"
$REGION = "<region>"

# Environment only (safest — leaves infra for reuse/redeployment)
python destroy.py --bootstrap-stack $STACK_NAME --region $REGION

# Also remove landing zone (NAT, Route Server, SG, key pair)
python destroy.py --bootstrap-stack $STACK_NAME --region $REGION --include-infra

# Full nuke — removes everything including VPC, runner, and the bootstrap stack
# In BYO-VPC mode, only resources created by this stack inside the VPC
# will be removed — nothing pre-existing is touched.
python destroy.py --bootstrap-stack $STACK_NAME --region $REGION --all
```
With `--include-infra`, the runner and the bootstrap stack remain (and the
runner keeps billing) until you run `--all` or delete the bootstrap stack.

Only requires `boto3` and AWS credentials. Auto-discovers the environment ID,
VPC, and landing-zone stack from the bootstrap stack. Shows a confirmation
prompt before proceeding (type `destroy`; skip with `-y`).

Other options: `--environment-id` (target an environment explicitly — needed if
the script reports `No active environment found`, since it then skips
environment teardown), `--skip-hosts` (the hosts are already deleted),
`--profile`, `--endpoint-url`.

If a resource fails to delete, the script logs a warning and continues rather
than stopping, so check for `DESTROY FINISHED WITH N WARNING(S)` — re-running is
safe, and anything still listed needs removing by hand.

### Safer order: environment first, then infrastructure

If `destroy.py` finds no environment, it skips the environment and, with
`--include-infra` or `--all`, still deletes the infrastructure. If the
environment was really there (for example the account has several and
discovery picked none), its hosts keep billing in a VPC that has lost its
supporting resources. To avoid that, especially in an account with more than
one EVS environment:

1. Delete any DNS records you added for extra hosts
   ([Adding or removing hosts](#adding-or-removing-hosts)).
2. Run the environment-only teardown with `--environment-id` set explicitly.
3. Confirm the environment and its hosts are gone. The first command must
   print `DELETED` or nothing, and the second nothing:

   ```bash
   aws evs list-environments --region $REGION \
     --query "environmentSummaries[?environmentId=='$ENVIRONMENT_ID'].environmentState" --output text
   aws evs list-environment-hosts --region $REGION --environment-id $ENVIRONMENT_ID \
     --query "environmentHosts[?hostState!='DELETED'].hostName" --output text
   ```

4. Only then run it again with `--include-infra` or `--all`.

### What's left after teardown

`destroy.py` deletes this environment's `evs-<environment-id>_*` appliance
secrets with Secrets Manager's default 30-day recovery window, so they can be
restored until then. These stay until you delete them yourself:

- the two CloudWatch log groups (kept when the stack is deleted, with 90-day
  retention);
- the S3 bucket and the uploaded blueprint, OVA, and ovftool;
- the depot token secret;
- the SNS topic, if you created one.

To confirm nothing is still billing:

```bash
aws evs list-environments --region $REGION
aws ec2 describe-instances --region $REGION --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,Tags[?Key==`Name`]|[0].Value]' --output table
aws ec2 describe-nat-gateways --region $REGION --filter Name=state,Values=available \
  --query 'NatGateways[].[NatGatewayId,VpcId]' --output table
```

## Notifications

Set `SnsTopicArn` at launch to get progress on your deployment via SNS:
when it **starts**, **after each stage completes**, **at least every 2 hours
during long stages** (bare-metal host creation and VCF bringup), and on final
**success or failure**. Every message is prefixed with the stack name, so if
several deployments publish to one topic you can tell them apart.

You receive them by subscribing an endpoint (email, SMS, chat, etc.) to the
topic. New to SNS? See [Getting started with Amazon
SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-getting-started.html) for
how to create a topic and add a subscription. If the topic uses a
customer-managed KMS key, grant the runner role `kms:GenerateDataKey` and
`kms:Decrypt` on that key.

## License

See [LICENSE](../../LICENSE).
