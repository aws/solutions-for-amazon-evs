# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
