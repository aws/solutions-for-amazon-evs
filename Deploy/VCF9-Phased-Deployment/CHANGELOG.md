# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

### Added

- Phase 1: Terraform-based AWS base infrastructure provisioning (VPC, subnets, Route Server, DNS, Transit Gateway, jumpbox)
- Phase 2: Python CLI for EVS environment deployment (create environment, create hosts, VLAN route table associations, EBS volume management)
- Phase 3: Python CLI for VCF bringup and NSX edge cluster deployment (depot management, bringup orchestration, 7-stage edge cluster deployment)
- One-shot `deploy-environment` action in Phase 2 for end-to-end EVS provisioning
- One-shot `deploy-vcf-and-edge` action in Phase 3 for end-to-end VCF + NSX deployment
- Support for both VCF 9.0 and VCF 9.1 target versions
- AWS Secrets Manager integration for VCF appliance password management
- Idempotent operations with automatic retry and exponential backoff
- Dry-run mode for all deployment actions
