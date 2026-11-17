# Discover prerequisites and missing choices

Read the selected EDO README's "Prerequisites", "Deployment options", and
relevant "Setup" sections. For an existing network, include "BYO-VPC".
Use these for the required inputs and their allowed values; do not copy
them into this skill.

## Establish the relevant context

Reuse the request and supplied files: those the user named or gave you.
A blueprint, plan, review directory, or AWS resource (such as an S3
object, secret, or topic) you only found is a candidate: list it and ask
whether to use it; take no values from it until the user says yes. Do not ask for the account, profile,
or Region before discovery. Establish the identity under core.md's
"Find the user's credentials", narrowing profiles with anything already
known from the request and supplied files. Propose the Region from the
profile's configuration or where the user's artifacts are, not from the
model provider's environment; ask only if these conflict or stay unknown.
If account access is unavailable, a local plan/blueprint can still proceed
with account checks marked unknown; deployment remains blocked.

Inspect the matching release README, template, and reference blueprint.
Check the release's local tools/dependencies and use an isolated environment
in the review directory for missing Python dependencies. Inspect an existing
clone without changing it. Dependency versions belong to the release, not to
a pin in this skill.

Before multi-step discovery, tell the user in one line what you are checking.

## Discover before asking

Run these read-only checks in order. Start with names and locations the
user supplied, but do not stop there; avoid unrelated accounts and
exhaustive inventories, and describe permission or search limits instead of
claiming something does not exist.

1. **Identity:** as in core.md, "Find the user's credentials". Unless the
   user named the profile or account in this conversation, stop after the
   identity calls and ask the credentials question, even if only one profile
   or the current credentials work, with the artifact question in formats.md
   if the user hasn't said where the files are; run no other check, AWS or
   local, until they answer. If they named the profile or account but not
   where the files are, ask the artifact question alone before check 2.
   Never rely on the shell's default Region; it may belong to the model
   provider. Pass `--region` on every regional command.
2. **Artifacts:** look only where the user said. For an S3 location, in
   this or another account, run `get-bucket-location` and `list-objects-v2`
   on it; for a bucket in another account, record a check that its bucket
   policy lets the EDO runner role read it. If the files aren't there, say
   so and ask before searching. If you can't read the location (for
   example, access denied), say so and ask the user for the OVA and ovftool
   file names; take the VCF version from the name they give, never assume
   it. Search only when the user says to: then
   `aws s3api list-buckets` and `list-objects-v2` on this account's buckets
   whose names mention EVS, EDO, or VCF, and nothing local. If that finds
   nothing, ask; don't look elsewhere. Take the VCF version from the OVA's
   file name; never download, open, or read inside the OVA, and ask when the
   name has no version. Settle the Region now: the user's, else
   the artifacts' bucket Region, never the EDO README's examples. Pass
   `--region` on every later command.
3. **Depot secret:** `aws secretsmanager list-secrets --region <Region>` and
   `describe-secret`, metadata only. If the template's default name is
   absent, list secrets whose names mention depot, VCF, Broadcom, or token as
   candidates and ask which to use; never report the secret missing after
   checking only the default name.
4. **Support:** `aws support describe-severity-levels`. Success shows one of
   the plans the [Support API requires](https://docs.aws.amazon.com/awssupport/latest/user/about-support-api.html);
   the user can confirm the plan name.
5. **Versions and hosts:** `aws evs get-versions` and
   `aws ec2 describe-instance-type-offerings`, intersected with release
   support; if `get-versions` is denied, offer only the release README's
   instance types. EDO uses EVS self-deployed mode, so managed VCF versions
   do not apply; use the ESXi builds. An offering is not a capacity
   guarantee. Compare versions literally; never treat a different version
   number as the same release (VCF 5.2 is not VCF 9.1). If the user asks
   for a VCF version the release doesn't support, say so; for versions EVS deploys itself, point to the User
   Guide's [Getting started](https://docs.aws.amazon.com/evs/latest/userguide/getting-started.html),
   and never suggest another EDO release supports it unless its README says so.
6. **Network and names:** VPC CIDRs and route destinations; for BYO-VPC,
   `describe-subnets` and `describe-internet-gateways`; existing EVS
   BYO-VPC requirements if one is used. Check names only after the user
   confirms them ([plan.md](plan.md)).
7. **Release:** run `git ls-remote` for the ref the template pins; empty
   output means it is not published, so record a launch blocker.
8. **Quotas:** EDO's quota tool, once a blueprint exists ([plan.md](plan.md)).
   Before then, as in a readiness check, run `aws service-quotas get-service-quota`
   for each quota the README's quota step names for this configuration, and
   report each limit; you don't need the user's sizes to read them.

Reuse valid artifacts where they are and keep optional choices from a
supplied blueprint. Where candidates differ meaningfully, present evidence
and resolve the choice. Distinguish missing permission from a failed
prerequisite.

## Propose one starting configuration

For values the user has not given, propose the same starting configuration
every time, labeled as defaults they can change. When the user asks for the cheapest or
smallest setup, say this is the smallest the release supports and why
(Simple mode, the minimum host count, the fewest-core instance type, optional
components off), and link the pricing page; no price figures or totals.

- **Deployment:** Simple mode with the release's minimum host count, for
  evaluation. Offer HA mode and its minimum as the production alternative.
  If an earlier answer in this conversation gave a different host minimum
  or VCF version, say why the release's values differ (EDO uses
  self-deployed mode).
- **Appliance sizes:** the release reference blueprint's values, shown as
  one "reference sizes" line rather than field by field.
- **Names:** one unused base name, `evs-env-01` or the next number, for the
  CloudFormation stack and the EVS environment; domain `<base>.internal`;
  the reference hostnames. Say the stack and environment names can differ,
  that neither can be changed after launch, and name each stack the release
  README says EDO creates for this configuration, such as
  `<stack>-amazon-evs-<version with dashes>-infrastructure`, with every
  part of the version EDO uses (for VCF 9.1.0, `-9-1-0-0-`),
  and any storage stack.
- **Required settings without a default** (for example principal storage on
  an instance type without local disks): no default. Ask, giving the values
  the release README allows and the User Guide's sizing guidance for that
  setting, with its source; an allowed minimum is not a recommendation.
  For each option, say what the README says it requires (for example, a
  mode that works only in a VPC the stack creates).
- **Network:** a new VPC on the template's default prefix when it does not
  overlap discovered or user-supplied ranges.
- **Optional components:** the release defaults, with their implications
  and which ones bill.
- **Instance type:** no default. List compatible types with physical cores
  and vCPUs. If the user asks for a recommendation or defers ("defaults are
  fine", "you pick"), propose the type with the fewest physical cores that
  meets the stated needs and ask them to confirm it; never choose it
  silently. A yes, or a second deferral, confirms the proposed type; say
  which type you are using. Cores set the VCF subscription size. Say it is not a workload
  sizing, and make no capacity claims.

## Present one consolidated discovery result

Use the discovery result format in [formats.md](formats.md).

Show relevant facts and choices already supplied or verified, what remains
missing/unknown/failed, and who resolves each gap. Separate approved-agent
setup candidates from user tasks such as authentication, entitled downloads,
secret values/creation, service agreements, or undiscoverable network ranges.

Then ask, in one numbered list and in this order, only what discovery did
not settle, at most six questions:
1. One confirmation for everything with a value: "OK to use this resource?"
   or "OK to use these resources?" listing the found artifacts and depot
   secret, the VCF version from the OVA's name, deployment type and host
   count, the rest of the starting configuration, UI access (jumpbox), and
   notifications. The user can change any item by name.
2. Names: the stack and environment name and the DNS domain, with the
   defaults below. Say neither name can change after launch, and name each
   stack EDO creates.
3. Instance type.
4. External ranges the network must avoid.
5. Anything discovery left open with no proposal, including each required
   setting without a default: its allowed values and the User Guide's sizing
   guidance, with the source, and no default.
6. The EVS terms: fetch the
   [CreateEnvironment API Reference](https://docs.aws.amazon.com/evs/latest/APIReference/API_CreateEnvironment.html)
   now and quote its `termsAccepted` description verbatim, never from
   memory, with the core count for each instance type offered so one
   confirmation covers the user's pick.

Skip any question already settled. Give each a one-line default,
stating its actual value, or its options, with a short explanation only
where a term needs one. Never default agreement to the terms. Report
release-ref problems as plan blockers, not questions. Follow up only for
unresolved items or new findings; do not make the user restate verified
setup.

Once the user answers, continue with [plan.md](plan.md) in the same reply:
write and validate the blueprint and show the plan. Open blockers, such as
an unpublished release, go into the plan; they do not stop this work.
