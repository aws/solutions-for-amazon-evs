# Message formats

Use these shapes so every conversation reads the same. Fill `<...>` from
discovery and the matching EDO release; leave out empty sections.

## Credentials and artifact questions

Ask these before discovery, in one message. Ask the credentials question
only when core.md's credential search leaves no single match, and the
artifact question only when the user hasn't said where the files are.

```text
Which AWS credentials should I use?
- Current credentials: <account> / <principal> (<source, e.g. AWS_PROFILE=name>)   <- only if they work
- Another profile: tell me its name, if you want to use one
If you sign in another way (for example SSO or a role), set it up in your
terminal and tell me when it's ready. Don't paste credentials here.

Where are the VCF Installer OVA and the Linux ovftool?
- An S3 location (in this or another account), or a local path
- Or say "search" and I'll look in this account's S3 buckets whose names
  mention EVS, EDO, or VCF
```

## Discovery result

```text
I've finished the read-only checks. I haven't made any changes to your account.

Found
| Item | Value | Source |        <- one row per checked item; Source is
|---|---|---|                       requested, discovered, or default

Warnings                         <- always: EDO doesn't check the OVA at launch, with the
                                    README's check for you to run; plus any checks not run
Blockers                         <- only if any: item, why, who resolves it

Questions
1. OK to use this resource / these resources? <list>   <- values that exist
2. <topic>: <question> Default: <value>.     <- no value yet; write
                                                "No default" where discover.md gives none
...
```

Ask only the topics listed in discover.md, in that order, numbered, and
nothing else in this round. Skip a topic the user already settled.

## Plan

Copy this block and fill in every line; write "none" for an empty one. Don't
shorten it, reorder it, or point to deployment-plan.md instead.

```text
EVS deployment plan: <stack name>

Target          <account> / <role> (profile <name>) · <Region> / <AZ> · EDO <ref> (<commit>)
Configuration   one line per value, labeled requested, discovered, or default; for a
                supplied blueprint, its path and what you changed in your copy
Checks          Passed: <list, each with its command>. Not checked: <list>.
Warnings        always include: EDO doesn't check the OVA at launch; a wrong file fails hours
                later at VCF bringup. Check it yourself: <the README's OVA check command>
Blockers        <list, each with who resolves it>, or none
Stacks          <stack> (you launch), then each stack the release README says EDO creates,
                by name (for example <stack>-amazon-evs-<version with dashes>-infrastructure)
AWS changes     numbered steps in order, AWS changes only (local file edits aren't steps);
                each shows its exact resolved command, then one line in plain words on
                what it does, what it costs, and whether it can be undone;
                values set before the commands (shell variables) are setup, not steps
Billing         each billable resource the release creates (hosts and the rest) and when it
                starts billing, before or after the hosts, and the pricing page link
Teardown        not part of this approval: the command for later, run only when you ask;
                what the release keeps by default, whether it keeps billing, and the option
                that removes it
Files           review directory and file names
```

## Decline or defer

```text
Nothing in your AWS account was created or changed. The review files are in
<folder>: <file names>.

For your review
- Billable steps: <step numbers and what bills from when>
- Cost drivers: <hosts × instance type, then each other billable resource>; <pricing page link>
- Teardown: <how>, and what it keeps by default and whether that keeps billing

Before resuming, I'll re-check the credentials, release, names, and terms.
```

## Confirmations

Use this wording, filling only the `<...>` parts.

- Terms: "The `termsAccepted` field in the CreateEnvironment API Reference
  (<link>) says: "<verbatim text>" With <hosts> × <instance type> that is
  <n> physical cores (not vCPUs). Do you confirm this?"
- Plan (ask even when blockers remain, and add "Launch waits for <blockers>."):
  "Reply 'approve' to run steps 1–<n> above, in order: <one-line list,
  for example: upload 3 files, check the template, launch the stack>. Nothing
  in your account changes until you do."

## Before sending

- Every question is from the discover.md list, numbered, in order.
- Each passed check names the command you ran; unrun checks say not checked.
- Terms text came from the page you fetched in this conversation.
- No capacity or sizing claims. Prices only as SKILL.md's sources rule
  allows: quoted and cited from the pricing page, with the reminder to confirm
  them there and in the Pricing Calculator; no totals.
- The blueprint and review files are written before the plan is sent, even
  with blockers.
- Before asking for approval, the whole plan is in this reply; a link to
  deployment-plan.md alone is not a plan. Each step shows its exact command.
- Warnings, in the discovery result and the plan, say that EDO doesn't check
  the OVA at launch, with the README's check for the user to run.
- Never offer to create, read, or handle a secret's value.
- "I haven't made any changes", "Nothing in your AWS account was created or
  changed" and "Nothing in your account changes until you do" are used only
  when they are true: check every command run in this conversation first.
  If any created, uploaded, changed, or deleted something, replace the line
  with a list of each change and the command that made it.
- A required setting without a default has the user's value, or is still a
  question; never one you picked.
- Release-ref problems sit under Blockers, and the plan is still shown.
- A denied or unavailable check is listed as not checked under Warnings;
  it never stops the blueprint or the plan.
