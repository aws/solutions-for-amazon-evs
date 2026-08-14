# Amazon EVS — Automated VCF 9 Install

Deploy a fully-configured VMware Cloud Foundation 9 environment on Amazon EVS by
launching a single CloudFormation stack and letting the automation run to
completion (~4-6 hours). No Terraform, no manual multi-phase workflow — you fill
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
progress. It takes about **4-6 hours**, and when it finishes you have a
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

1. **AWS account** with an active Business or Enterprise Support plan.
2. **Broadcom depot token** — from the [Broadcom Support Portal](https://support.broadcom.com) under your VCF entitlement, to enable VCF downloads.
3. **VCF SDDC Manager OVA** — download from the [Broadcom Support Portal](https://support.broadcom.com) under your VCF entitlement (e.g. `VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova`). Supported VCF versions: **9.0.2** and **9.1.0** — the OVA version must match `evs.vcf_version` in your blueprint.
4. **VMware ovftool** (Linux zip) — download from the [Broadcom Support Portal](https://support.broadcom.com) (e.g. `VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip`).
5. A machine with the **OVA and ovftool downloaded locally**, to upload them in
   step 4. If you follow the CLI commands rather than the console, you'll also
   need the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
   configured for the target account, plus `python3` (3.9+) for the optional
   quota check in step 3. Note that AWS CloudShell's home directory is too
   small for the OVA.

The setup steps below walk you through uploading these, creating
the secret, customizing the blueprint, and launching.

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
4. **The deployment runs unattended for ~4-6 hours.** Bare-metal hosts are
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
repo and `cd` into this tool's folder once, up front:
```bash
git clone https://github.com/aws/solutions-for-amazon-evs.git
cd solutions-for-amazon-evs/Deploy/EVS-Deployment-Orchestrator
```
Run every command below from this directory (`Deploy/EVS-Deployment-Orchestrator/`).

### 1. Create the depot token secret
One-time setup. Set your variables and run:
```bash
SECRET_NAME=evs-depot-token   # can be whatever you want — must match DepotSecretName at launch
DEPOT_TOKEN=YOUR-TOKEN
REGION=us-east-2

aws secretsmanager create-secret \
  --name $SECRET_NAME \
  --secret-string "$DEPOT_TOKEN" \
  --region $REGION
```
> The secret can have any name — just make sure the `DepotSecretName` stack
> parameter matches it when you launch (default: `evs-depot-token`).

### 2. Pick and customize a blueprint
Start from a ready-to-go blueprint in the [`blueprints/`](blueprints/) folder that
matches your instance type and VCF version, copy it to `blueprint.yaml`, and
change the two `CHANGE ME` fields. Every field is documented inline.

Blueprint options:

- `blueprints/i4i.metal.vcf90.vsan.example.yaml` — i4i.metal, VCF 9.0.2, vSAN
- `blueprints/i4i.metal.vcf91.vsan.example.yaml` — i4i.metal, VCF 9.1.0, vSAN
- `blueprints/i7i.metal-24xl.vcf90.vsan.example.yaml` — i7i.metal-24xl, VCF 9.0.2, vSAN
- `blueprints/i7i.metal-24xl.vcf91.vsan.example.yaml` — i7i.metal-24xl, VCF 9.1.0, vSAN
- `blueprints/custom.all-options.example.yaml` — every option, commented out, for a fully custom config

Copy your pick to `blueprint.yaml` in the current directory, e.g.:
```bash
cp blueprints/i4i.metal.vcf91.vsan.example.yaml blueprint.yaml
```
Then open `blueprint.yaml` and change the two `CHANGE ME` fields.

With a ready-to-go blueprint the only fields you must change are:

- `dns.fqdn` — any private domain name you choose (e.g. `"vcf.mycompany.internal"`)
- `evs.environment_name` — display name for this environment (letters, digits, hyphens, underscores only)

`instance_type` and `vcf_version` are already set to match the blueprint you
picked. Need more (or fewer) hosts? Each blueprint's `hostnames.esxi` list has
inline guidance on changing the host count.

### 3. Check your account has room (optional but recommended)
Now that you have a real blueprint, verify your account has the quota headroom
this deployment needs — bare-metal vCPUs, VPC/subnet/route table/security
group/NAT gateway limits, EVS environment and host-count limits, Route 53
hosted zones and Resolver endpoints, and Secrets Manager. Set your variables
and run:

```bash
BLUEPRINT=blueprint.yaml
REGION=us-east-2
AZ=us-east-2a

pip3 install boto3   # skip if you already have it (preinstalled in AWS CloudShell)
python3 check-quotas.py --blueprint $BLUEPRINT --region $REGION --availability-zone $AZ
```

It compares what your blueprint needs against your service quotas **and**
what's already in use, and prints exact remediation commands for any
shortfall (add `--byo-vpc` if deploying into an existing VPC, plus
`--existing-vpc-id` for fully accurate per-VPC results). `--availability-zone`
should match the AZ you'll pass at launch — it scopes the NAT-gateway check,
which is skipped otherwise. Read-only — it changes nothing in your account.

### 4. Upload the blueprint, OVA, and ovftool to S3
`$OVA` and `$OVF` below must point to where you actually downloaded them —
either `cd` there first, or set the variables to the full path (e.g.
`OVA=~/Downloads/VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova`). `$BLUEPRINT`
is the `blueprint.yaml` you created in step 2, in your current directory.

Set the bucket and region first — run this whether or not you need to create a
bucket, since the upload commands below use both:
```bash
BUCKET=my-evs-deployment-bucket   # an existing bucket, or a new globally-unique name
REGION=us-east-2
```
If you don't already have a bucket, create it now (bucket names must be
globally unique, so pick something distinctive):
```bash
aws s3api create-bucket --bucket $BUCKET --region $REGION \
  --create-bucket-configuration LocationConstraint=$REGION
```
In `us-east-1`, omit the `--create-bucket-configuration` argument — that region
rejects an explicit `LocationConstraint`.

Then upload the three files. Use the OVA version matching the blueprint you
copied in step 2 (a 9.1.0 blueprint needs a 9.1.0 OVA):
```bash
BLUEPRINT=blueprint.yaml
OVA=VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova
OVF=VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip

aws s3 cp $BLUEPRINT s3://$BUCKET/blueprint.yaml --region $REGION \
  && aws s3 cp $OVA s3://$BUCKET/ --region $REGION \
  && aws s3 cp $OVF s3://$BUCKET/ --region $REGION
```
Console: S3 → Create bucket (skip if you already have one) → open it → Upload (repeat for all three files).

### 5. Launch the stack

Create a stack from `evs-deployment-orchestrator.yaml` in the CloudFormation console
(Stacks → Create stack → Upload a template file), or via CLI.

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
```bash
STACK_NAME=my-evs-deployment
REGION=us-east-2
BUCKET=my-evs-deployment-bucket
OVA=VCF-SDDC-Manager-Appliance-9.1.0.0.xxxxx.ova
OVF=VMware-ovftool-4.6.3-xxxxx-lin.x86_64.zip
AZ=us-east-2a
SECRET_NAME=evs-depot-token   # the depot-token secret you created above

aws cloudformation create-stack \
  --stack-name $STACK_NAME \
  --region $REGION \
  --template-body file://evs-deployment-orchestrator.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=BlueprintKey,ParameterValue=s3://$BUCKET/blueprint.yaml \
    ParameterKey=OvaKey,ParameterValue=s3://$BUCKET/$OVA \
    ParameterKey=OvfKey,ParameterValue=s3://$BUCKET/$OVF \
    ParameterKey=AvailabilityZone,ParameterValue=$AZ \
    ParameterKey=DepotSecretName,ParameterValue=$SECRET_NAME
```

**Optional parameters** are added as extra `ParameterKey=...,ParameterValue=...`
entries on the same `--parameters` list. Two you may want:

```bash
    # Get email/SMS progress notifications (see "Notifications" below):
    ParameterKey=SnsTopicArn,ParameterValue=arn:aws:sns:$REGION:<account-id>:my-topic \
    # Bring your own EC2 key pair instead of letting the stack create one:
    ParameterKey=KeyPairName,ParameterValue=my-existing-key \
```

> **Bringing your own key pair?** `KeyPairName` must be an EC2 key pair that
> **already exists in this region**, and it is used for **both** SSH to the
> runner and RDP to the Windows jumpbox. **Keep the `.pem` file** — AWS does not
> store the private key for a key pair you created, so it is *not* in SSM
> Parameter Store, and you need it to decrypt the jumpbox's Windows
> Administrator password (see [Windows jumpbox](#windows-jumpbox-optional)).
> If you omit `KeyPairName`, the stack creates `<stack-name>-runner-key` for you
> and puts its private key in SSM, which is the easier path.

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

### Telling "failed" apart from "still working"

A normal deployment takes 4–6 hours and individual stages are legitimately slow
— host creation is typically 30–90 minutes and VCF bringup 2–4 hours — so long
gaps with no output are expected. What to look for instead:

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

```bash
KEY_FILE=<key-file>
RUNNER_PUBLIC_IP=<runner-public-ip>

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
| Tear down | See "Teardown" section below |

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

Console path: EC2 → Instances → `evs-jumpbox` → Security tab → open its
security group → Edit inbound rules → Add rule: RDP, source "My IP". Then
Instance → Connect → RDP client → Get password (upload the key from SSM
Parameter Store `/ec2/keypair/<key-id>`).

RDP to `$JUMPBOX_IP` as `Administrator`, open a browser, and the
`https://vc.<your-fqdn>` links from the success email work directly — DNS and
routing inside the VPC are already wired up.

## File layout

```
├── README.md                  ← you are here
├── evs-deployment-orchestrator.yaml         ← the CloudFormation template (launch this)
├── blueprints/                ← ready-to-go blueprints (pick one, customize it)
│   ├── i4i.metal.vcf90.vsan.example.yaml
│   ├── i4i.metal.vcf91.vsan.example.yaml
│   ├── i7i.metal-24xl.vcf90.vsan.example.yaml
│   ├── i7i.metal-24xl.vcf91.vsan.example.yaml
│   └── custom.all-options.example.yaml   ← every option, commented, for custom configs
├── check-quotas.py            ← pre-launch quota preflight (run this)
├── destroy.py                 ← standalone teardown script
├── orchestrator/              ← the deployment automation (runs on the runner)
│   ├── deploy_orchestrator.py
│   ├── evs_environment/       ← EVS environment + host creation
│   └── vcf_deployment/        ← VCF bringup, depot, edge, connector
└── spec-generator/            ← interactive blueprint/spec builder (optional helper)
```

## BYO-VPC

Use an existing VPC instead of letting the stack create one. Start from
whichever step matches what you already have — skip anything already done.

### Step 1: VPC (skip if you already have one)
```bash
REGION=us-east-2
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.30.0.0/16 --region $REGION \
  --query 'Vpc.VpcId' --output text)
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-support --region $REGION
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames --region $REGION
```
Console: VPC → Your VPCs → Create VPC → VPC only → CIDR `10.30.0.0/16`.
Enable DNS resolution and DNS hostnames under Actions → Edit VPC settings.

### Step 2: Internet gateway (skip if your VPC already has one)
```bash
IGW_ID=$(aws ec2 create-internet-gateway --region $REGION \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --vpc-id $VPC_ID --internet-gateway-id $IGW_ID --region $REGION
```
Console: VPC → Internet gateways → Create → then Actions → Attach to VPC.

### Step 3: Subnets (create any you don't already have)

You need 3 subnets, all in the **same AZ**:

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
Console: VPC → Subnets → Create subnet (repeat 3x). Then select each of the
runner/public subnets → Actions → Edit subnet settings → enable auto-assign public IP.

### Step 4: Route tables

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
Console: VPC → Route tables → Create (2x). For the public one: Routes tab →
Edit → add `0.0.0.0/0` → target: your IGW. For both: Subnet associations →
Edit → check the appropriate subnets.

### Step 5: Use these values at stack launch

```bash
echo "ExistingVpcId:                     $VPC_ID"
echo "ExistingRunnerSubnetId:            $RUNNER_SUBNET"
echo "ExistingServiceAccessSubnetId:     $SERVICE_SUBNET"
echo "ExistingServiceAccessRouteTableId: $SERVICE_RT"
echo "ExistingPublicSubnetId:            $PUBLIC_SUBNET"
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

2. **Add an inbound SSH rule to the runner's security group** (scoped to your IP):
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

3. **Connect:**
   ```bash
   RUNNER_PUBLIC_IP=<runner-public-ip>

   ssh -i runner-key.pem ec2-user@$RUNNER_PUBLIC_IP
   ```
   The runner's public IP is visible in the EC2 console or via:
   ```bash
   RUNNER_INSTANCE_ID=<runner-instance-id>
   REGION=<region>

   aws ec2 describe-instances --instance-ids $RUNNER_INSTANCE_ID \
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
```bash
STACK_NAME=<stack-name>
REGION=<region>

# Environment only (safest — leaves infra for reuse/redeployment)
python3 destroy.py --bootstrap-stack $STACK_NAME --region $REGION

# Also remove landing zone (NAT, Route Server, SG, key pair)
python3 destroy.py --bootstrap-stack $STACK_NAME --region $REGION --include-infra

# Full nuke — removes everything including VPC and runner
# In BYO-VPC mode, only resources created by this stack inside the VPC
# will be removed — nothing pre-existing is touched.
python3 destroy.py --bootstrap-stack $STACK_NAME --region $REGION --all
```
Only requires `boto3` and AWS credentials. Auto-discovers the environment ID,
VPC, and landing-zone stack from the bootstrap stack. Shows a confirmation
prompt before proceeding (skip with `-y`).

Other options: `--environment-id` (target an environment explicitly — needed if
the script reports `No active environment found`, since it then skips
environment teardown), `--skip-hosts`, `--profile`, `--endpoint-url`.

If a resource fails to delete, the script logs a warning and continues rather
than stopping, so check for `DESTROY FINISHED WITH N WARNING(S)` — re-running is
safe, and anything still listed needs removing by hand.

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
