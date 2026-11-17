# Amazon EVS Agent Skill: guided EDO deployments

An Agent Skill that helps your agent complete an Amazon EVS Deployment
Orchestrator (EDO) deployment using your own AWS credentials.

Ask it to deploy an environment, check readiness, or prepare a blueprint
and plan. It gathers requirements, checks prerequisites, resolves missing
choices, and prepares a reviewable plan. After you approve, it performs the
planned setup and EDO launch and follows progress as its harness permits.
It also answers factual Amazon EVS questions from official documentation.

## What to expect

| Phase | What happens |
|---|---|
| Prepare | The agent verifies existing inputs/setup, discovers missing prerequisites, gives one consolidated findings/questions response, and validates a blueprint and approval plan |
| Run | It performs remaining approved setup and launch actions, records results, and hands off the actual deployment |
| Monitor | It observes progress, verifies completion, and helps diagnose errors and plan documented recovery |

You can bring your own blueprint, completed prerequisites, an approved plan,
or an existing deployment. The agent starts from that state, verifies what
applies, and carries completed work forward. It asks only for missing choices.

If you ask only for readiness or a plan, it stops at that scope. The skill
does not calculate workload host requirements or quote final prices; host
type and count follow your confirmed requirements. It does not replace your
licensing contact or provide ongoing VCF administration.

Use a capable model. The skill is tested with Claude Sonnet 4.6 and Claude
Haiku 5.5 or later; smaller models may skip checks or report results they
did not verify.

## Prerequisites and authority

Follow the **Prerequisites** in EDO's README at the selected release:
https://github.com/aws/solutions-for-amazon-evs

That README defines supported inputs, required artifacts and tools, setup
commands, durations, and resource lifecycle. The agent reads its relevant
sections at the same release as the code it will run.

You authenticate your usual way. The agent uses a profile you name, or
finds the matching one among your active credentials and named profiles
(checking only each profile's identity), and asks only if more than one
could fit. It never asks you to paste AWS credentials or secret values. You obtain entitled artifacts and handle service agreements and
secret creation where needed. Agent-resolvable setup is included in the
plan for approval.

The plan names the account, Region, deployment choices, and exact changes,
including the CloudFormation stack you launch (default `evs-env-01`; you can
rename it) and the infrastructure stack EDO creates from it.
Account changes wait for your agreement. If the scope changes or execution
fails, the agent diagnoses read-only and asks about the proposed recovery.
Your harness's permissions still apply; the skill does not bypass them.

## Progress across turns

The agent keeps local review files and a `deployment-state.json` record with
configuration references, check results, approved scope, resource IDs, and
progress. It stores no secret values. In later turns, it reconciles the
record with live state before continuing, so completed actions are not
repeated. The record itself does not grant permission to change resources.

Automatic progress reports depend on the agent's scheduling or streaming
capabilities. If those are unavailable, it explains the limitation and
provides the monitoring commands and a way to return for status checks.

## Fewer permission prompts

Your agent decides when to ask before running a command; the skill can't
change that. `SKILL.md` lists its read-only checks in `allowed-tools`, but
agents treat that field differently: some ignore it, and some apply it only
briefly. If prompts slow you down, use your agent's own permission
settings to allow the read-only AWS commands you trust (the `allowed-tools`
list is a starting point) or to turn on an automatic approval mode if it
has one. Commands that change resources still follow the reviewed plan.

## Install

The skill is the `amazon-evs-agent-skill` folder (the one that holds
`SKILL.md`). Its name must stay `amazon-evs-agent-skill`: an Agent Skill's
folder name has to match the `name` in `SKILL.md`.

Copy the folder into your agent's skills directory. Use the personal location
to have it in every project, or the project location to keep it with one
repository.

| Agent | Personal location | Project location |
|---|---|---|
| Claude Code | `~/.claude/skills/amazon-evs-agent-skill/` | `.claude/skills/amazon-evs-agent-skill/` |
| Kiro | `~/.kiro/skills/amazon-evs-agent-skill/` | `.kiro/skills/amazon-evs-agent-skill/` |
| OpenAI Codex | `$HOME/.agents/skills/amazon-evs-agent-skill/` | `.agents/skills/amazon-evs-agent-skill/` |

For example, for Claude Code, from the root of this repository:

```bash
mkdir -p ~/.claude/skills
cp -R skills/amazon-evs-agent-skill ~/.claude/skills/
```

Other agents that support Agent Skills work the same way: copy the folder into
that agent's skills directory (see its documentation).

**While you edit the skill,** link it instead of copying it, so your agent
always sees the latest version:

```bash
ln -s "$PWD/skills/amazon-evs-agent-skill" ~/.claude/skills/amazon-evs-agent-skill
```

### Check that it's installed

| Agent | How |
|---|---|
| Claude Code | Type `/` and look for `amazon-evs-agent-skill`. A new skill is picked up in the current session; if the top-level skills directory didn't exist when the session started, run `/reload-skills` |
| Kiro | Type `/` and look for `amazon-evs-agent-skill` |
| Codex | Run `/skills`, or type `$`, and look for `amazon-evs-agent-skill` |

Then try a question, such as "What VLAN subnets does Amazon EVS create when I
create an environment?", or a read-only request, such as "check whether my
account is ready to deploy Amazon EVS". Neither creates anything.

## Update or remove

| To | Do |
|---|---|
| Update | Replace the `amazon-evs-agent-skill` folder with the new version (or `git pull`, if you linked it), then start a new agent session |
| Remove | Delete the `amazon-evs-agent-skill` folder from your skills directory |

## If it doesn't show up

- The folder must be named `amazon-evs-agent-skill`, and `SKILL.md` must be
  directly inside it (not in a subfolder).
- Check that the skills directory is the right one for your agent (table
  above), and start a new session.
- If an EDO script fails on import, check the selected release README's
  local tool and dependency requirements.

## What's in the folder

| Path | Purpose |
|---|---|
| `SKILL.md` | Routing and the rules for every request |
| `references/edo/core.md` | EDO rules for every phase: identity, approvals, release selection, context between phases |
| `references/edo/prepare.md` | Preparation entry point for any starting state |
| `references/edo/discover.md` | Scoped discovery and consolidated findings/missing decisions |
| `references/edo/plan.md` | Blueprint/parameter validation and the approval plan |
| `references/edo/formats.md` | Discovery result, plan, and confirmation message formats |
| `references/edo/run.md` | Remaining approved setup and EDO launch |
| `references/edo/monitor.md` | Progress, completion evidence, and error routing |
| `references/edo/recover.md` | Diagnosis and documented recovery |
| `references/edo/operate.md` | Access, host changes, and teardown |
| `references/knowledge.md` | Documentation Q&A lookup and answer formats |

When maintaining the skill, keep EDO facts and commands in the tool's README.
Keep rules for every request in `SKILL.md` and EDO-wide rules in
`references/edo/core.md`; other references contain only their task-specific
guidance. Read only the references and README sections the current task needs.

## Learn more

- Amazon EVS documentation: https://docs.aws.amazon.com/evs/
- EDO repository: https://github.com/aws/solutions-for-amazon-evs
- Agent Skills specification: https://agentskills.io/specification
