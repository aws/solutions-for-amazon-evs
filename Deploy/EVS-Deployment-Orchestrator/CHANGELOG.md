# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

