# Changelog

All notable changes to the Amazon EVS Agent Skill (`amazon-evs-agent-skill`)
are recorded here. The skill ships
with the EVS solutions repo and has the repo's version, shown as
`metadata.version` in `SKILL.md`.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the repo follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First release, shipping with EVS solutions v1.1.0.

### Added

- Q&A: answers factual Amazon EVS questions from the
  [Amazon EVS documentation](https://docs.aws.amazon.com/evs/) and cites the
  page for each answer. When the documentation doesn't cover a question, it
  says so instead of answering from model knowledge.
- EDO deployments, in four phases:
  - Prepare: read-only discovery of the account, Region, EVS versions and
    instance types, VPCs, quotas, artifacts and the depot secret, then a
    validated blueprint, launch parameters and a deployment plan for review.
  - Run: after the user approves the plan, the setup and stack launch the plan
    names.
  - Monitor: progress from the orchestrator log, completion checks, and
    diagnosis with the README's documented recovery steps.
  - Operate: access to the environment, adding or removing hosts, and teardown.
- FSx for NetApp ONTAP principal storage: plans name every stack EDO creates,
  including the storage stack, say when each billable resource starts billing,
  and take teardown facts (the storage stack is kept and keeps billing unless
  `--delete-storage` is given) from the release README. Required FSx settings
  are asked for, with the User Guide's sizing guidance, not defaulted.
- Approval gate: nothing in AWS is created, uploaded, changed or deleted before
  the user approves the plan step that names it.
- Secrets: metadata only; the skill never asks for, fetches, prints or stores
  secret values.
- Release pinning: the skill reads EDO's README at the release it deploys.
- `allowed-tools` in `SKILL.md` lists the read-only AWS CLI commands the skill
  runs, so agents that honor the field can pre-approve them.
- Requirements: Q&A needs a web fetch tool and access to `docs.aws.amazon.com`.
  Deployments need AWS CLI v2 or the AWS MCP server, `git`, and Python 3.10 or
  later with `boto3` and `pyyaml`.
