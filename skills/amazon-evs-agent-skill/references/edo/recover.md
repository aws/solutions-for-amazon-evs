# Diagnose an error and recover

Use the rules in [SKILL.md](../../SKILL.md) and [core.md](core.md) and establish the live
target/deployed release. Read its README's "Troubleshooting" and
"Before you resume". Use [knowledge.md](../knowledge.md) for factual EVS
questions while keeping the deployment context.

Read logs, stack events, and environment/host state to locate the failure.
Report the observed stage and supporting evidence, resources still active
or billable, the needed correction, and the proposed recovery's exact target,
commands, scope, and destructive effects.

If EDO cannot perform the correction through its documented capabilities,
explain what must resolve it. Do not replace or patch the tool.

Once recovery is agreed, re-check deployed state, perform the correction and
documented resume, and record results. An ambiguous result remains unresolved
until live evidence clarifies it. Return to [monitor.md](monitor.md).
