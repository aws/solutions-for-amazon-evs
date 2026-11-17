# Run the approved deployment

Use the rules in [SKILL.md](../../SKILL.md) and the rules and progress record
in [core.md](core.md).
Read the matching EDO README's "Setup" upload/launch instructions.

## Establish readiness

Locate the reviewed plan, blueprint, launch parameters, template, and check
results. If the user arrives with an existing plan or blueprint, establish
what is validated and approved. Use [prepare.md](prepare.md) only for missing
decisions/checks; do not restart discovery or overwrite their configuration.

Verify agreement for the current files and target. Inspect live state for
actions already completed by the user or an earlier turn; reuse their results
and verify readiness rather than recreating resources.

## Execute the remaining actions

1. Run the approved prerequisite/setup actions in their planned order.
   Record each result and resolve dependent identifiers from its output.
   Explain customer-only actions, such as email subscription confirmation.
2. Verify pending prerequisites after setup and re-check affected evidence.
   Launch needs completed required checks, available quota headroom, and
   explicit service-term acceptance; approved preparation alone is insufficient.
3. Launch EDO through the README's entry point with the reviewed template
   and complete parameter file. Record the returned stack ID.
4. Inspect the created stack's parameters against the approved configuration.
   Then hand its identifiers, log location, and latest state to
   [monitor.md](monitor.md).

Report each action/result as it runs. Shell variables may not persist across
tool calls, so use the recorded paths and resolved values.

On a failure or ambiguous result, stop subsequent writes and reconcile live
state under the rules in core.md. Diagnose with [recover.md](recover.md).
Do not launch another stack to compensate for uncertainty.

This phase ends with a verified launch and monitoring handoff, or the exact
blocker and next action. A deployment already launched goes directly to
monitoring.
