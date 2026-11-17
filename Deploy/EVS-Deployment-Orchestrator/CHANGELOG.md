# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `storage.principal` blueprint field: `type: "vsan"` (the default, the same as leaving `storage` out) or `type: "fsxn"`, FSx for NetApp ONTAP as the management cluster's datastore, for instance types with no local disks. The FSx settings go in a `storage.principal.fsxn` block. Without `fsxn.volume_id` the orchestrator creates a Single-AZ file system in the service access subnet, in a VPC the stack creates, and every Quick create field is required; with `fsxn.volume_id` (needs BYO-VPC) it uses your own volume and never modifies or deletes it, and ignores any creation fields with a warning. Whether an instance type has local storage is read from `ec2:DescribeInstanceTypes` at launch. Works with every supported VCF 9 version. A simple deployment with `type: "fsxn"` needs 2 hosts instead of 3, and the VCF NFS network goes on the host management subnet (VLAN 0, MTU 1500), as in the Amazon EVS User Guide
- `check-quotas.py` checks FSx for ONTAP quotas when `storage.principal` creates a file system
- The orchestrator-created FSx storage stack gets CloudFormation termination protection once a deployment succeeds. Teardown (`destroy.py` and the runner's `--destroy`) keeps it by default; `--delete-storage` turns the protection off and deletes it after the environment. `destroy.py --all` needs `--delete-storage` when the storage stack exists. Teardown deletes the volume without a final backup (`SkipFinalBackup`), so no FSx backup is left billing, and waits up to 2 hours for the file system to delete. It never lists or deletes backups
- VCF 9.1.1: `evs.vcf_version: "9.1.1"`, with the VCF 9.1.1 component bundles pinned and the 9.1.1 SDDC Manager OVA

### Fixed

- README CLI launch: upload the template to S3 and create the stack with `--template-url`. The template is over the 51,200-byte `--template-body` limit, so the documented command failed

### Security

- `urllib3` 2.8.0 in the VCF deployment stage (`orchestrator/vcf_deployment/requirements.txt`), for the HTTPS proxy TLS and chunked-response issues fixed in that release

## [1.0.0]

### Added

- Blueprint version guard: a blueprint's `requires.edo` range (for example `">=1.0.0, <2.0.0"`) is checked against the orchestrator's `VERSION` at launch, and a blueprint outside the range stops before anything is deployed. A blueprint without `requires` is treated as written for v1.0.0 (`">=1.0.0, <2.0.0"`). An unknown top-level section is a warning. The orchestrator logs its version at start
- CloudFormation bootstrap template (`evs-deployment-orchestrator.yaml`) that deploys a fully-configured VMware Cloud Foundation 9 environment on Amazon EVS from a single stack launch
- Bootstrap runner instance that executes the deployment orchestrator unattended, generating configuration from a user-customized blueprint (see `blueprints/`)
- Automated creation of the EVS environment and bare-metal ESXi hosts (instance type and count configurable in the blueprint)
- Automated VCF 9 installation and configuration: vCenter Server, NSX Manager, SDDC Manager, and VCF Operations
- NSX edge cluster deployment for connectivity between the NSX overlay network and the VPC
- Deployment networking provisioned automatically (DNS, NAT gateway, Route Server, security group) via an orchestrator-created infrastructure stack
- Appliance credential management in AWS Secrets Manager (`evs-<environment-id>_<role>` secrets)
- `check-quotas.py` pre-launch preflight that compares blueprint requirements against service quotas and current usage, with remediation commands
- `destroy.py` standalone teardown script supporting environment-only, infrastructure, and full (`--all`) cleanup back to a non-billing state
- Optional Windows jumpbox (`jumpbox.enabled` in the blueprint) for reaching the vCenter/NSX/SDDC Manager UIs inside the VPC
- BYO-VPC support: deploy into an existing VPC by providing `ExistingVpcId` and subnet/route-table parameters
- Support for both VCF 9.0 and VCF 9.1 target versions
- Deployment monitoring via CloudWatch Logs and optional SNS start/failure/success notifications (`SnsTopicArn`)
- Resume support for failed deployment stages via the orchestrator's `--resume` flag on the runner
- ESXi build selection matches the VCF version's major.minor.patch when `evs.esx_version` isn't set, so VCF 9.1.0 gets an ESXi 9.1.0 build and not a newer patch the VCF Installer rejects. If no build matches, the deployment stops and lists the available builds

