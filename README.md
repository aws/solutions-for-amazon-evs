# **Amazon Elastic VMware Service**

## Solutions for Amazon EVS

This repository contains automation solutions for [Amazon Elastic VMware Service (Amazon EVS)](https://aws.amazon.com/evs/), a service that enables customers to run VMware Cloud Foundation (VCF) workloads natively on AWS infrastructure.

### What's included

| Solution | Description |
|----------|-------------|
| [EVS Deployment Orchestrator](Deploy/EVS-Deployment-Orchestrator/) | Automated, single-CloudFormation-stack VCF 9 install — fill in a few parameters, launch one stack, and it builds a complete Amazon EVS + VCF 9.0 or 9.1 environment end-to-end (AWS networking, bare-metal ESXi hosts, vCenter/NSX/SDDC Manager/VCF Operations, and an NSX edge cluster) unattended in ~4–6 hours. |

### Why automate?

Installing VCF manually involves dozens of configuration steps, carefully crafted JSON specs, password generation, and multi-hour wait times. The automation toolkit presented here reduces that to a short, repeatable setup and a single CloudFormation stack launch:

- **Repeatability** — Deploy identical environments across regions and accounts
- **Speed** — Reduce a days-long manual process to one unattended stack launch
- **Auditability** — Every configuration decision is captured in version-controlled code
- **Error reduction** — Eliminate manual typos in DNS records, VLAN CIDRs, and bringup specs
- **Security** — Passwords generated automatically and stored in AWS Secrets Manager

## License

This project is licensed under the Apache-2.0 License. See the [LICENSE](LICENSE) file.
