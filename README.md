# **Amazon Elastic VMware Service**

## Solutions for Amazon EVS

This repository contains automation solutions for [Amazon Elastic VMware Service (Amazon EVS)](https://aws.amazon.com/evs/), a service that enables customers to run VMware Cloud Foundation (VCF) workloads natively on AWS infrastructure.

### What's included

| Solution | Description |
|----------|-------------|
| [VCF 9 Phased Deployment](Deploy/VCF9-Phased-Deployment/) | End-to-end automation for provisioning an Amazon EVS environment and deploying VMware Cloud Foundation 9.0.x, including AWS networking infrastructure, EVS environment creation, VCF bringup, and NSX edge cluster deployment. |

### Why automate?

Installing VCF manually involves dozens of configuration steps, carefully crafted JSON specs, password generation, and multi-hour wait times. The automation toolkit presented here reduces that to three CLI commands:

- **Repeatability** — Deploy identical environments across regions and accounts
- **Speed** — Reduce a multi-hour manual process to three commands
- **Auditability** — Every configuration decision is captured in version-controlled code
- **Error reduction** — Eliminate manual typos in DNS records, VLAN CIDRs, and bringup specs
- **Security** — Passwords generated automatically and stored in AWS Secrets Manager

## License

This project is licensed under the Apache-2.0 License. See the [LICENSE](LICENSE) file.