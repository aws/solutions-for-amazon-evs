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
