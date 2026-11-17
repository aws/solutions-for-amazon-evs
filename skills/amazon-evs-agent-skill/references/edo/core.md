# EDO core rules for every phase

Apply these with the rules in [SKILL.md](../../SKILL.md) in every EDO phase.

## Working rules

- **Use the user's identity.** Use the profile established under
  [Find the user's credentials](#find-the-users-credentials). Verify account
  and role/user before account work and before every AWS mutation; stop on
  an unexpected change. A refreshed session for the same role is expected.
  Pass the agreed profile and Region explicitly. Do not switch profiles
  without agreement, read credential files, print credential variables, mint
  credentials, assume another role, or sign in/refresh for the user. They
  authenticate their usual way when needed.
- **Separate discovery from changes.** Read-only checks, local dependency
  setup, and local review files may proceed without an AWS approval prompt.
  Explain the checks and their results; an unavailable or denied check is
  unknown, not passed. Do not claim the user's credentials are read-only.
  Run each read-only check as a single command without pipes, so the
  user's pre-approvals match it; limit output with `--query`/`--output`.
  Write a blueprint only from values the user gave or accepted in answer
  to the discovery questions; a request for a new blueprint starts those
  questions. A question about a file never changes it.
- **Execute the agreed plan.** Approval covers its named AWS changes,
  including setup, uploads, permissions, and launch. Run them without
  repeated approval prompts. Verify the reviewed files still match; changed
  scope/target (including a different bucket, key, or Region) or recovery
  after failure needs agreement. A status request
  or saved record grants no mutation authority. Respect harness permissions;
  for a blocked step, provide its resolved command and verify user-run results.
- **Use existing commands, not new code.** Use released EDO tools and
  documented APIs with values filled in for the user's shell. Do not write
  helper programs/scripts, modify EDO code, or bypass its validators.
  Blueprints, parameter files, plans, and progress records are data files.
- **Make choices and evidence clear.** Distinguish requested values,
  observations, and proposed defaults. Explain consequential choices.
  Do not invent workload sizing, prices, licensing interpretation, or
  acceptance of service terms. Link official pricing pages; never call
  pricing APIs, compute costs, or choose options by price. Use only the
  starting configuration in
  [discover.md](discover.md) as defaults, labeled as such,
  never other example values. In any blueprint draft, keep
  `evs.terms_accepted: false` until the user confirms the EVS terms as
  described in [plan.md](plan.md).
- **Confirm destructive scope.** Identify the exact environment and affected
  resources. Teardown removes and verifies the environment first, then
  separately approves infrastructure removal. Never stop or terminate EVS
  hosts through EC2 or suppress deletion prompts without an explicit request.

## Find the user's credentials

Use a profile the user names in this conversation or the deployment
record names; `AWS_PROFILE`, environment credentials, and the default
chain are active credentials, not a named profile. Otherwise find
candidates yourself: the active credentials only (a profile's own name is not
a hint). Before asking, run one `aws sts get-caller-identity` on the active
credentials, only when the request needs AWS and the session has AWS access.
If the session has no AWS access, make no AWS call: say so and ask the
credentials question. Make no other AWS call and no profile listing or config read
before the answer, including with `--profile default`. The question offers the
active credentials and lets the user name another profile; list none.

Use a candidate without asking only when the user's request or files point
to its account or profile and no other candidate could match; a candidate
whose check failed (for example, no network) is not ruled out. State the
profile, account, principal, and why. Otherwise, before any other check,
ask the credentials question in [formats.md](formats.md); working active
credentials are an option to offer, not a choice to make. If the chosen identity's first checks are denied or
find nothing for EVS, report that and ask instead of continuing. If several
fit or none work, list only the working ones with account and principal, summarize failures in one line by
cause, and ask which to use; ask nothing that discovery would answer. Never
choose by name alone. Probe only with that identity call, never read
credential files, and never sign in or refresh for the user.

Name the cause instead of "no credentials": expired or invalid token means
the user re-authenticates that profile; `NoCredentials` with profiles
present means select a profile; no profiles visible when the user expects
some means the shell may be sandboxed from their AWS configuration; an
unreachable endpoint or unresolved host means the agent's sandbox may
block network access, which the user allows in their agent's settings;
`AccessDenied` later means that check is unknown.

## Use the matching EDO release

EDO's README owns technical prerequisites, inputs, defaults, commands,
validation, timings, and lifecycle. Read only relevant sections of
`Deploy/EVS-Deployment-Orchestrator/README.md`; do not copy its procedures
or dependency/version tables into the skill.

Use the requested release, otherwise the available template's named runner
ref. If neither exists, discover and propose a stable published release.
Verify the public ref; never silently choose `main` or a prerelease. Never
bypass a release blocker with testing-only template parameters or packaged
local code; that needs the user's explicit choice and stays a plan warning.

Use one matching checkout for README, blueprint reference, validators, and
template; verify runner and blueprint compatibility. Preserve the user's
clone and use a separate checkout if needed. Record ref and resolved commit;
stop launch on a mismatch or missing documentation. Cite EDO files at that ref.
EDO's public repository is https://github.com/aws/solutions-for-amazon-evs.
With no user checkout, check the release ref there with `git ls-remote`. Clone
it, or compare a local checkout with it, only when the user asks; don't ask the
user to fetch or provide a checkout. Without a matching checkout, read the
release's files at its tag instead of cloning:
`https://raw.githubusercontent.com/aws/solutions-for-amazon-evs/<tag>/Deploy/EVS-Deployment-Orchestrator/<file>`
for `README.md`, the template, the reference blueprint, `tools/validate_blueprints.py`,
and `spec-generator/constants.py` (which the validator imports). Save them in
the deployment folder in the same layout and run the validator there. List
any check whose files you couldn't read as not checked under Warnings.

For an existing deployment, establish account, Region, stack/environment,
deployed release, and current state from live read-only evidence. Do not
assume a local checkout describes it. Resolve an ambiguous target before
changing it.

## Carry context between phases

Keep review files together: `deployment-plan.md`, the working blueprint,
launch parameters, and `deployment-state.json`. Use one folder per
deployment, `evs-deployments/<environment-name>/` in the current
workspace, keep any isolated Python environment there too, and tell the
user its path. Don't write elsewhere in the EDO checkout or edit its files. The record stores
paths and checksums of reviewed files, identity/location/release, check
evidence and blockers, current-conversation approval scope, action results/resource IDs,
and latest observations. Update it after material changes; avoid duplicating
the full plan or blueprint.

Reconcile it with the conversation, current files, identity, and live state
before continuing. Re-check evidence affected by changed inputs or stale
account state. Resolve identifiers returned by approved actions only through
the bindings described in the plan. Never repeat a create/upload solely
because a local record is missing. After an ambiguous write, inspect live
state before considering a retry; record an unresolved outcome as unknown.
