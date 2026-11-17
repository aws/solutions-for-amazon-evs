# Agent skills for Amazon EVS

This folder contains [Agent Skills](https://agentskills.io/specification) for
Amazon Elastic VMware Service (Amazon EVS). An agent skill is a folder of
instructions that your AI agent (for example Claude Code, Codex, or Kiro)
loads when a task matches the skill's description.

| Skill | What it does | Use when |
|---|---|---|
| [Amazon EVS Agent Skill](amazon-evs-agent-skill/SKILL.md) | One skill with two capabilities. **Q&A** answers Amazon EVS questions from the official [Amazon EVS documentation](https://docs.aws.amazon.com/evs/), with a source for every claim, and says it doesn't know when the documentation doesn't answer. **Deployment** plans, deploys, monitors, troubleshoots, and tears down VCF 9 on Amazon EVS with the [EVS Deployment Orchestrator](../Deploy/EVS-Deployment-Orchestrator/) (EDO). See its [README](amazon-evs-agent-skill/README.md). | You have a question about Amazon EVS, or you want to evaluate, plan, deploy, or operate an EVS environment. |

> [!NOTE]
> The agent skills were tested with frontier models across several agentic
> coding assistants. Smaller models are less reliable; review the agent's plans
> and commands before running them.

## Install

Copy or link the skill folder into your agent's skills folder. For example:

| Agent | Project skills folder |
|---|---|
| Claude Code | `.claude/skills/` |
| Agents that use the shared convention (for example Codex) | `.agents/skills/` |
| Kiro | `.kiro/skills/` |

For example, from the root of this repository:

```bash
mkdir -p .claude/skills
cp -R skills/amazon-evs-agent-skill .claude/skills/
```

Then ask your agent an Amazon EVS question, for example "What VLAN subnets
does Amazon EVS create when I create an environment?" The skill's
[README](amazon-evs-agent-skill/README.md#install) covers personal install locations, linking
while you edit, and what a deployment needs.

## Requirements

- An agent that supports Agent Skills.
- For Q&A: network access to `https://docs.aws.amazon.com`, and a web fetch
  tool the agent can use.
- For Deployment: see [What you need](amazon-evs-agent-skill/README.md#prerequisites-and-authority).
