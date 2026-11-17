# Changelog

All notable changes to the EVS solutions repo are recorded here. The repo has
one version for all of its solutions, so each release has one entry, and each
change names the solution it belongs to (`edo:` is the EVS Deployment
Orchestrator). Each solution also keeps its own `CHANGELOG.md` in its folder.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the repo follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Every release is also on the
[Releases page](https://github.com/aws/solutions-for-amazon-evs/releases).

## [Unreleased]

## [1.1.0]

Adds support for `r7i.metal-24xl` and `r7i.metal-48xl` hosts, which have no
local disks, by using Amazon FSx for NetApp ONTAP for the VCF management
datastore. Also adds VCF 9.1.1, and the Amazon EVS Agent Skill, which enables
an AI agent to assist with Amazon EVS questions and use the tooling in this GitHub
repository.

### Added

- edo: `r7i.metal-24xl` and `r7i.metal-48xl` hosts, with Amazon FSx for NetApp
  ONTAP as the management cluster's datastore. Set it in the new, optional
  `storage` section of the blueprint: `storage.principal.type: "fsxn"`, with
  the FSx settings in a `storage.principal.fsxn` block. Without `storage`, or
  with `type: "vsan"`, the orchestrator deploys vSAN as before. Diskless
  instance types need `type: "fsxn"`, because vSAN needs local NVMe. With FSx
  for NetApp ONTAP, a simple deployment needs only 2 hosts.
- edo: the EVS Deployment Orchestrator (EDO) can create the FSx for NetApp
  ONTAP file system for you. To have EDO create it, leave out
  `fsxn.volume_id` and set every field in the `fsxn` block:
  `file_system_name`, `storage_capacity_gb`, `throughput_capacity_mbps`,
  `storage_efficiency` and `s3_access`. The file system is Single-AZ, in the
  VPC the stack creates, and is created before any host is deployed.
- edo: use an existing FSx for NetApp ONTAP volume by providing a valid
  `fsxn.volume_id` and an existing VPC (`ExistingVpcId`). The orchestrator
  checks the volume, its VPC, its Availability Zone, and its security group
  rules before creating any host. It will never change or delete the existing
  volume. Creation fields are ignored with a warning if the `fsxn.volume_id`
  is present.
- edo: once a deployment succeeds, the stack that holds the
  orchestrator-created file system gets CloudFormation termination protection.
- edo: `destroy.py` and the runner's `--destroy` keep the orchestrator-created
  file system, which keeps billing. Add `--delete-storage` to delete it with
  the datastore and every VM on it. `destroy.py --all` needs `--delete-storage`
  when that file system exists. Teardown deletes the volume without a final
  backup, so no FSx backup is left billing, and waits up to 2 hours for the
  file system to delete. Backups you made yourself are never touched.
- edo: `check-quotas.py` checks FSx for ONTAP quotas when the orchestrator will
  create a file system.
- edo: VCF 9.1.1 (`evs.vcf_version: "9.1.1"`), with the 9.1.1 SDDC Manager
  OVA.
- edo: the README adds sections on monitoring a deployment, troubleshooting,
  and adding or removing hosts.
- skills: the Amazon EVS Agent Skill (`skills/amazon-evs-agent-skill/`)
  follows the [Agent Skills specification](https://agentskills.io/specification).
  It works with agents that support Agent Skills.
- skills: Q&A answers factual Amazon EVS questions from the
  [Amazon EVS documentation](https://docs.aws.amazon.com/evs/) and cites the
  page for each answer. When the documentation doesn't cover a question, it
  says so instead of answering from model knowledge.
- skills: EDO deployments: drives an EDO deployment end to end with the user's
  AWS credentials, in four phases:
  - Prepare: read-only discovery of the account, Region, EVS versions and
    instance types, VPCs, quotas, artifacts, and the depot secret. It then
    creates a validated blueprint, launch parameters, and a deployment plan
    for review.
  - Run: after the user approves the plan, performs the setup and stack launch
    the plan names.
  - Monitor: follows progress in the orchestrator log, confirms completion,
    and diagnoses failures with the README's documented recovery steps.
  - Operate: access to the environment, adding or removing hosts, and
    teardown.
- skills: guardrails: the skill asks you to approve each change before it
  makes it in your AWS account, never reads secret values, and saves its
  progress in a local, git-ignored folder so a later session can continue.
- skills: release pinning: the skill reads EDO's README at the release it
  deploys, so its commands and prerequisites match that release.
- skills: `SKILL.md` lists the read-only AWS CLI commands the skill runs in
  `allowed-tools`, so agents that honor the field can pre-approve them.
- skills: `AGENTS.md` routes Amazon EVS requests in this repository to the
  skill.
- skills: requirements: Q&A needs a web fetch tool and access to
  `docs.aws.amazon.com`. Deployments need AWS CLI v2 or the AWS MCP server,
  `git`, and Python 3.10 or later with `boto3` and `pyyaml`.
- skills: install: copy `skills/amazon-evs-agent-skill/` to your agent's
  skills folder. `skills/amazon-evs-agent-skill/README.md` has the details.

### Fixed

- edo: the README's CLI launch uploads the template to your S3 bucket and
  creates the stack with `--template-url`. The template is larger than the
  51,200 bytes CloudFormation accepts with `--template-body`, so the
  documented command failed.

### Security

- edo: `urllib3` 2.8.0 in the VCF deployment stage, for the HTTPS proxy TLS
  and chunked-response issues fixed in that release.

## [1.0.0]

First stable release. It contains one solution, the EVS Deployment Orchestrator
(`Deploy/EVS-Deployment-Orchestrator/`). From a single CloudFormation stack, it
creates the EVS environment and its bare-metal ESXi hosts, installs VCF 9
(vCenter Server, NSX Manager, SDDC Manager and VCF Operations), and deploys an
NSX edge cluster. It supports VCF 9.0.2 and 9.1.0 on `i4i.metal`,
`i7i.metal-24xl` and `i7i.metal-48xl` hosts.

### Breaking changes

- The VCF 9 Phased Deployment is removed. The EVS Deployment Orchestrator
  replaces it, and there is no in-place upgrade: start a new deployment with
  the orchestrator. The last version with the phased deployment is v0.1.0.
- edo: the four instance-type example blueprints are removed. Copy
  `blueprints/custom.all-options.example.yaml` and set `evs.instance_type` and
  `evs.vcf_version` in it.

### Added

- A `VERSION` file at the repo root, with a copy in each solution folder, shows
  which release you have.
- edo: the template's description starts with the version, so the
  CloudFormation console shows which release a stack came from.
- The root README has a support matrix of the VCF versions and instance types
  each release supports, and a "Get help" section.
- edo: a blueprint can declare the orchestrator releases it works with, such as
  `requires: { edo: ">=1.0.0, <2.0.0" }`. At launch the orchestrator stops,
  before deploying anything, when its version is outside the range, and names
  the release you need. A blueprint without `requires` is treated as written
  for v1.0.0 (`">=1.0.0, <2.0.0"`), so it runs on every 1.x release. The
  orchestrator logs its version, and warns about top-level
  blueprint sections it doesn't use.

### Changed

- edo: the template runs the release it came from, not `main`. `PINNED_COMMIT`
  is the release tag, `v1.0.0`, so a stack launched later, or updated,
  runs the code its template was tested with. To get newer code, use the
  template from a newer release.
- edo: the README documents `i7i.metal-48xl` and starts every deployment from
  `blueprints/custom.all-options.example.yaml`.
- edo: when the blueprint doesn't set `evs.esx_version`, the orchestrator
  picks the newest ESXi build for the VCF version's major.minor.patch, so a
  VCF 9.1.0 deployment gets an ESXi 9.1.0 build. Before, it matched only
  major.minor and could pick a newer ESXi patch, such as 9.1.1, that the VCF
  Installer rejects. If no build matches, the deployment stops and lists the
  available builds.

## [0.1.0]

Historical and unsupported. This is the repo as it was just before the VCF 9
Phased Deployment was removed. Use v1.0.0 or later for new deployments.

### Added

- VCF 9 Phased Deployment (`Deploy/VCF9-Phased-Deployment/`): stands up an
  Amazon EVS environment and brings up VCF 9 in three phases that you run one
  after another. Phase 1 creates the base infrastructure with Terraform, phase 2
  creates the EVS environment and hosts, and phase 3 brings up VCF and the NSX
  edge cluster.
- An early version of the EVS Deployment Orchestrator
  (`Deploy/EVS-Deployment-Orchestrator/`).
