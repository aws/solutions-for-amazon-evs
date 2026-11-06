# **Amazon Elastic VMware Service**

## Solutions for Amazon EVS

This repository contains automation solutions for [Amazon Elastic VMware Service (Amazon EVS)](https://aws.amazon.com/evs/), a service that enables customers to run VMware Cloud Foundation (VCF) workloads natively on AWS infrastructure.

### What's included

| Solution | Description |
|----------|-------------|
| [EVS Deployment Orchestrator](Deploy/EVS-Deployment-Orchestrator/) | Automated, single-CloudFormation-stack VCF 9 install — fill in a few parameters, launch one stack, and it builds a complete Amazon EVS + VCF 9.0 or 9.1 environment end-to-end (AWS networking, bare-metal ESXi hosts, vCenter/NSX/SDDC Manager/VCF Operations, and an NSX edge cluster) unattended in ~4–7 hours. |

### Why automate?

Installing VCF manually involves dozens of configuration steps, carefully crafted JSON specs, password generation, and multi-hour wait times. The automation toolkit presented here reduces that to a short, repeatable setup and a single CloudFormation stack launch:

- **Repeatability** — Deploy identical environments across regions and accounts
- **Speed** — Reduce a days-long manual process to one unattended stack launch
- **Auditability** — Every configuration decision is captured in version-controlled code
- **Error reduction** — Eliminate manual typos in DNS records, VLAN CIDRs, and bringup specs
- **Security** — Passwords generated automatically and stored in AWS Secrets Manager

## Versions

The repo is released as a whole: every solution in it ships under the same
version, and solutions from the same release were tested together. Versions
follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) and
look like `vMAJOR.MINOR.PATCH`, such as `v1.2.3`. Each release is a git tag
with a GitHub Release on the
[Releases page](https://github.com/aws/solutions-for-amazon-evs/releases), and
its notes are also in [CHANGELOG.md](CHANGELOG.md).

| Release | What it means for you | Example |
|---------|-----------------------|---------|
| MAJOR, such as `v2.0.0` | Something you use is removed or works differently. The release notes say what to do | A solution or example blueprint is removed; a blueprint field is renamed or becomes required |
| MINOR, such as `v1.1.0` | Something is added. What you already use keeps working | A new solution; a new supported instance type or VCF version; a new optional blueprint field |
| PATCH, such as `v1.0.1` | Fixes only. Inputs and outputs don't change | A bug fix; a README correction |

A release candidate, such as `v2.0.0-rc.1`, comes before a MAJOR release so you
can test it first. It's published as a Pre-release and is never Latest.
Released versions never change: a fix ships as a new release.

### Supported versions

| Release | Solution | VCF versions | Instance types | Status |
|---------|----------|--------------|----------------|--------|
| [v1.0.0](https://github.com/aws/solutions-for-amazon-evs/releases/tag/v1.0.0) | [EVS Deployment Orchestrator](Deploy/EVS-Deployment-Orchestrator/) | 9.0.2, 9.1.0 | `i4i.metal`, `i7i.metal-24xl`, `i7i.metal-48xl` | Supported |
| [v0.1.0](https://github.com/aws/solutions-for-amazon-evs/releases/tag/v0.1.0) | VCF 9 Phased Deployment, and an early EVS Deployment Orchestrator | 9.0, 9.1 | See the release | Historical, unsupported |

Each release's template runs the code of that release: the EVS Deployment
Orchestrator template from `v1.0.0` checks out the `v1.0.0` tag, never `main`. Fixes ship in new releases. Older releases stay downloadable as they are, and
fixes are not backported to them.

### Stay informed

To get an email for each new release, choose **Watch → Custom → Releases** at
the top of this page.

### Find the version you're on

- The `VERSION` file, at the repo root and in each solution folder, holds the
  version, such as `1.0.0`.
- For the EVS Deployment Orchestrator, the stack's description in the
  CloudFormation console starts with the version:
  "Amazon EVS Deployment Orchestrator v1.0.0".
- In a clone of this repo, `git describe --tags` names the release you're on,
  or the release your commit came after.

### Use a release with git

```bash
git clone https://github.com/aws/solutions-for-amazon-evs.git
cd solutions-for-amazon-evs

git tag --list 'v*' --sort=-v:refname   # list releases, newest first
git checkout v1.0.0                     # switch to a release (a read-only "detached HEAD")
git switch -c my-v1.0.0 v1.0.0          # or start your own branch from a release

git fetch --tags                        # later: get releases published since you cloned
git describe --tags                     # name the release you're on
git log --oneline v1.0.0..v1.1.0        # what changed between two releases
git diff v1.0.0 v1.1.0 -- Deploy/EVS-Deployment-Orchestrator/blueprints/   # what changed in the blueprints
```

`main` has the latest merged changes, including ones not yet in a release.
Deploy from a release tag, not from `main`. To download a release without git, use the source code archive on its Releases page entry.

### Previous versions

Every release stays on the Releases page, and you can download it as it was.
[v0.1.0](https://github.com/aws/solutions-for-amazon-evs/releases/tag/v0.1.0)
is the repo just before the VCF 9 Phased Deployment was removed. It's historical
and unsupported: use the EVS Deployment Orchestrator for new deployments.

## Get help

| I want to… | Go to |
|------------|-------|
| Report a bug | [Open a bug report](https://github.com/aws/solutions-for-amazon-evs/issues/new?template=bug_report.md) |
| Request a feature, or vote on one | [Open a feature request](https://github.com/aws/solutions-for-amazon-evs/issues/new?template=feature_request.md), or add a 👍 to an existing one |
| Ask how to use a solution | [Open an Issue](https://github.com/aws/solutions-for-amazon-evs/issues/new/choose) |
| Ask about the Amazon EVS service itself | [AWS re:Post](https://repost.aws/) |
| Get help with an outage or with my account | An [AWS Support](https://console.aws.amazon.com/support/home) case |
| Report a security issue | The [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Please don't open a public Issue |

The solutions in this repo are supported on a best-effort basis through GitHub
Issues, with no SLA. Include the release you're on (see "Find the version
you're on") when you open an Issue.

## License

This project is licensed under the Apache-2.0 License. See the [LICENSE](LICENSE) file.
