# Prepare

Use the rules in [SKILL.md](../../SKILL.md) and [core.md](core.md).

Start with the requested outcome and whatever the user already has:
blueprint, artifacts, resources, previous checks, or a plan. Use the same preparation checks for every starting point:

1. **Discover and resolve.** Read [discover.md](discover.md) when inputs,
   account readiness, or required resources are unresolved. Verify and reuse
   existing setup; find missing artifacts/resources before asking the user
   for values you can discover. Present findings and missing decisions together.
2. **Validate and plan.** Read [plan.md](plan.md) once enough choices are
   known to review a configuration. Validate the supplied blueprint or
   prepare one, resolve remaining checks, and show the approval plan.

These steps may iterate when a check reveals a gap. A complete supplied
blueprint can go directly to validation and relevant account checks; it
does not need a separate questionnaire or a replacement blueprint.

Preparation ends with a reviewed configuration, evidence, and a concrete
plan, or identified blockers and their owners. A readiness/plan-only request
stops at its requested output. For a deployment, proceed to [run.md](run.md)
only within the agreed scope.
