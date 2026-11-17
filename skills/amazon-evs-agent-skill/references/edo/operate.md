# Access, host changes, and teardown

Use the rules in [SKILL.md](../../SKILL.md) and the live-target checks in
[core.md](core.md).
Read only the matching deployed release README sections for the requested
operation. These are targeted operations using the same discover, plan,
approve, execute, and verify approach; do not restart deployment preparation.

## Access

Use "After deployment", "Windows jumpbox (optional)", and
"Other ways to allow RDP". Present only relevant access paths. Prefer an
existing organizational managed prefix list, then a user-selected IP;
offer the documented Session Manager path where applicable. Never open
RDP to the world.

Discover resources and specify any security-group/IAM change, access source,
and later rule removal in the agreed plan. Give resolved credential-retrieval
instructions for the user to run themselves.

## Host changes

Use "Adding or removing hosts", including the documented VCF-side
prerequisites. Identify the environment and host, plan the exact change,
perform the approved documented action, and verify the result.

## Teardown

Use "Teardown", "Safer order", and "What's left after teardown".
Identify environment, associated stacks, and removal scope. Follow the entry
skill's staged approval/removal rule. Verify environment removal completed
before proposing infrastructure removal; an empty result for an ambiguous
target is insufficient evidence.

Before proposing a teardown, list what the release keeps by default, whether
each still bills, and the option that removes it. Report retained resources,
continuing charges, warnings, and the documented manual cleanup that remains;
the check that nothing is left billing covers the kept resources too. Update
the shared record with observed results.
