# Validate the configuration and present the plan

Read the matching README's blueprint/reference guidance, "Choosing the ESXi
build", "Check the blueprint", quota check, "Other checks before launch",
upload/launch instructions, and "Teardown" with "What's left after
teardown". These supply validation rules and commands. State what teardown
deletes, keeps, and keeps billing only from that README section, in
discovery as well as in the plan.

## Prepare review files

Use a dedicated review directory. Write the blueprint, launch parameters,
and plan even while blockers remain: blockers stop the launch, not the
review files. If the user supplied a blueprint, validate
it and work on a copy only when changes are needed. Retain valid choices and
optional fields; resolve missing or incompatible fields. Otherwise build a
blueprint from the release reference, the user's answers, and the starting
configuration in [discover.md](discover.md), including every section the
release validator requires, even when disabled. Label each value as
requested, discovered, or default. Prepare
`launch-parameters.json` from the matching template and launch instructions.

Run the README's structure validator and manual checks (except the OVA
content check, which the user runs), ESXi/artifact checks,
quota tool with the actual AZ and BYO arguments, and applicable account/name
checks for this configuration. Reuse still-applicable evidence; after an edit,
repeat affected local checks before account checks that consume the blueprint.
Run documented checks rather than writing new validators or quota arithmetic.

Also check what the documented tools may miss:

- The ESXi build EDO would select matches the OVA version, for supplied
  blueprints too; pin `evs.esx_version` when they differ.
- Blueprint compatibility explicitly covers the selected runner release
  through `requires.edo` when supported; otherwise state the limitation.
  Check unique DNS labels and sizing values against the release reference.
- Proposed S3 keys and template-derived key-pair names do not collide with
  existing resources; never upload over an existing object without the
  user's agreement. Optional/billable choices match the requested scope.
- Compare a supplied artifact checksum. Warn that EDO doesn't check the OVA
  at launch, so a wrong file fails hours later at VCF bringup, and give the
  README's OVA check for the user to run; don't run it yourself.
- The confirmed names are unused: stacks, EVS environments, and a Route 53
  hosted zone named `dns.fqdn`. Say the hosted-zone check is there because
  EDO creates a new private zone with that name in this account for the VCF
  hostnames, and that it doesn't use DNS the user runs elsewhere.
- The release ref exists publicly (`git ls-remote`); otherwise launch is
  blocked. A template of up to 51,200 bytes is validated read-only now. A
  larger one can only be validated after an S3 upload, so before approval
  report its size only, and list "create the bucket (if needed), upload the
  template, validate it" as numbered plan steps that run after approval,
  then launch with `--template-url`.
- Launch parameters carry every selected value whose omission changes the
  configuration; do not rely on example-command defaults.
- `evs.terms_accepted` reflects the user's own confirmation in this
  conversation. Never copy `true` from a reference, example, or CI
  blueprint; keep it `false` until the user confirms the terms.

Record results and their evidence. For each gap, identify the responsible
person/action and whether it blocks launch. Planned setup is not a completed
prerequisite; a quota request is not granted headroom. Required failed/unknown
checks and unresolved user requirements remain launch blockers.

## Present the approval plan

Re-read formats.md's Plan block now; the reply must copy it line for line.
Write `deployment-plan.md` and initialize the progress record described in
[core.md](core.md). Present a readable summary of:

- Target account/principal/profile, Region/AZ, release/commit, and goal.
- The configuration, including hosts, software versions, network/addressing,
  optional components, access, artifacts, and secret references.
- Verified checks, warnings, missing user actions, and launch blockers.
- The EVS terms: quote the current `termsAccepted` description from the
  [CreateEnvironment API Reference](https://docs.aws.amazon.com/evs/latest/APIReference/API_CreateEnvironment.html)
  with that link and the resulting physical core count, and ask the user to
  confirm it themselves unless they already did so in this conversation. A
  `true` in a supplied blueprint still needs this confirmation, and a change
  to host count or instance type resets it to `false` until the user
  confirms the new core count. Record it
  in the progress record; without it, launch is blocked.
- The exact sequence of proposed AWS changes, resource names, permissions,
  resolved commands, billing implications, and whether each can be undone
  (in words; the teardown command goes in the plan's Teardown line, outside
  the steps being approved).
- Monitoring capability, expected duration, completion evidence, and resources
  retained or still billable after the intended cleanup scope.

Use the matching README and its official sources for technical/cost claims.
Separate agent actions from customer tasks. For identifiers produced by an
approved action, describe the output binding used by later commands.
Show proposed defaults and meaningful changes to supplied configuration.

Present it and request agreement in the formats in
[formats.md](formats.md). When blockers remain, make clear which named
preparation actions can run and why launch must wait. Preserve the reviewed
files and approval in the shared record. Continue with [run.md](run.md) for
an approved deployment; otherwise hand over the requested files or readiness
report without AWS changes. If the user declines or defers, change nothing,
and use the decline format in [formats.md](formats.md).
