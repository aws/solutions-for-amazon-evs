# Phase 1 - Base Infrastructure

Terraform code that provisions the AWS infrastructure required before deploying an Amazon EVS environment.

## Modules

### `base_aws_infrastructure`

Creates the core networking and supporting resources:

- VPC with DNS hostnames and support enabled
- Service access subnet and public subnet
- Internet Gateway + NAT Gateway
- Route tables and routes (public and private)
- DHCP options set (domain name, DNS servers, NTP)
- Route 53 forward and reverse private hosted zones with A and PTR records
- Route 53 inbound resolver endpoint
- VPC Route Server with two endpoints, two peers, and route propagation
- EC2 key pair (server-side generated, private key stored in SSM)
- Transit Gateway and VPC attachment (optional)

### `jumpbox` (optional)

Creates a Windows Server jumpbox for RDP access:

- Dedicated public subnet with route to the Internet Gateway
- Security group allowing all traffic from within the VPC (add your IP manually for RDP)
- Key pair (server-side generated, private key stored in SSM)
- Windows Server 2025 EC2 instance (`t3.2xlarge` by default) with public IP, encrypted root volume, and IMDSv2 required

## Prerequisites

- Terraform 1.5+
- AWS credentials configured (via `AWS_PROFILE`, environment variables, or an assumed role)
- An IAM user or role with the permissions in [`iam_policy.json`](./iam_policy.json)
- Service-linked roles for Route Server, EVS, and Transit Gateway (auto-created on first deployment)
- **Clean VPC:** If reusing an existing VPC, ensure it has no conflicting
  subnets in the CIDR ranges used by EVS VLANs (`10.0.10.0/24` through
  `10.0.100.0/24` with default `cidr_prefix`). Existing subnets may cause
  Terraform to fail or require manual cleanup before re-applying.

## Setup

1. Copy the example tfvars file and fill in your values:

   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```

2. Set your AWS profile:

   ```bash
   export AWS_PROFILE=your-profile-name
   ```

## Deploy

```bash
# Initialize Terraform providers and modules
terraform init
# Preview the infrastructure that will be created
terraform plan
# Deploy all Phase 1 infrastructure (VPC, subnets, DNS, Route Server, security groups)
terraform apply
```

### Validate

After `terraform apply` succeeds, confirm key outputs are populated:

```bash
terraform output vpc_id
terraform output service_access_subnet_id
terraform output service_access_route_table_id
terraform output region
terraform output fqdn
```

All values should be non-null. If `create_jumpbox = true`:

```bash
terraform output jumpbox_public_ip
terraform output jumpbox_instance_id
```

## Optional Components

| Variable                     | Default      | Description                                           |
|------------------------------|--------------|-------------------------------------------------------|
| `create_tgw`                 | `false`      | Create a Transit Gateway and VPC attachment           |
| `create_jumpbox`             | `true`       | Create the Windows jumpbox instance                   |
| `jumpbox_instance_type`      | `t3.2xlarge` | EC2 instance type for the jumpbox                  |
| `enable_public_hcx`          | `false`      | Provision IPAM-backed public `/28`, EIP, and Network ACL for the HCX VLAN. When `true`, Phase 2's `pre-evs-sync-config` consumes the resulting `hcx_public_cidr` / `hcx_network_acl_id` / `hcx_eip_allocation_id` outputs and the `associate-hcx-eip` action attaches the EIP to the EVS HCX VLAN. |

Flip any of these to `true` in `terraform.tfvars` and run `terraform apply` again.

## Accessing the Jumpbox

The jumpbox is required for Phase 3 manual pre-work and script execution.

### 1. Add RDP inbound rule

The security group is created with no RDP ingress by default. Add your IP:

```bash
MY_IP=$(curl -s ifconfig.me)
SG_ID=$(terraform output -raw jumpbox_security_group_id)
REGION=$(terraform output -raw region)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp \
  --port 3389 \
  --cidr ${MY_IP}/32 \
  --region $REGION --profile <your-profile>
```

### 2. Retrieve the SSH key

```bash
KEY_PAIR_ID=$(terraform output -raw jumpbox_key_pair_id)
aws ssm get-parameter \
  --name /ec2/keypair/$KEY_PAIR_ID \
  --with-decryption \
  --region $REGION \
  --profile <your-profile> \
  --query 'Parameter.Value' \
  --output text > jumpbox.pem
chmod 400 jumpbox.pem
```

### 3. Get the Windows Administrator password

> Password data may take 4-10 minutes to become available after first launch.

```bash
aws ec2 get-password-data \
  --instance-id $(terraform output -raw jumpbox_instance_id) \
  --priv-launch-key jumpbox.pem \
  --region $REGION \
  --profile <your-profile>
```

### 4. Connect via RDP

```bash
terraform output jumpbox_public_ip
```

Open Microsoft Remote Desktop → Add PC → enter the public IP → connect
as `Administrator` with the decrypted password from step 3.

## Destroy

```bash
terraform destroy
```

## Outputs

Key outputs used by Phase 2 and Phase 3:

- `region` — AWS region
- `fqdn` — Phase 1's chosen FQDN (propagates into DNS + bringup spec)
- `vpc_id` — Underlay VPC ID
- `vpc_cidr_block` — VPC CIDR (used to derive VLAN subnets in Phase 2)
- `service_access_subnet_id` — Subnet ID for EVS service access
- `service_access_route_table_id` — Route table that Phase 2 associates every EVS VLAN subnet with
- `route_server_endpoint01_ip` / `route_server_endpoint02_ip` — Route Server endpoint ENIs that Phase 3's NSX edges peer to via BGP
- `route_server_peer01_id` / `route_server_peer02_id` — BGP peer IDs
- `vpc_default_security_group_id` — Default security group (used by jumpbox for ingress rule)
- `evs_security_group_id` — EVS service-access security group (used by EVS and the R53 resolver)
- `key_pair_name` — ESXi host key pair name
- `vcf_hostnames` / `esxi_hostnames` — short hostnames that flow into the VCF bringup spec

Phase 2 reads these directly from `terraform.tfstate` via the `pre-evs-sync-config` action.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `terraform apply` deploys to wrong region | Ran with placeholder tfvars | Edit `terraform.tfvars`, then `terraform destroy` + re-apply |
| Subnet conflicts on apply | Reusing a VPC with existing subnets | Delete conflicting subnets manually, or use a fresh VPC |
| Jumpbox password not available | Takes 4-10 min after first launch | Wait and retry `aws ec2 get-password-data` |
| `key pair ... already exists (AlreadyExists)` | Key pair from prior run still exists in EC2 | Delete the key pair: `aws ec2 delete-key-pair --key-name <name> --region <region>`, then re-apply. Or import it: `terraform import module.base_aws_infrastructure.awscc_ec2_key_pair.evs <key-pair-id>` |
| `Route Server is not associated with a VPC (InvalidRequest)` | Route Server propagation attempted before VPC association completes | Re-run `terraform apply` — the association will complete on retry. If persistent, check Route Server state in console |
| `terraform destroy` hangs on Route Server | AWS dependency ordering | Delete Route Server peers first via console, then retry destroy |

### Rollback

```bash
terraform destroy   # removes all Phase 1 infra
```

