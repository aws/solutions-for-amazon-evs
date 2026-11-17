# Monitor and verify the deployment

Use the rules in [SKILL.md](../../SKILL.md) and the rules and progress record
in [core.md](core.md).
Establish the live target and deployed release before observing an existing
deployment. Read that release README's "Monitoring & recovery" and
"Notifications" for commands, stages, timing, and completion signals.

## Observe and report

Discover logs through the actual stack and confirm its environment. Use the
orchestrator log as primary progress evidence; runner tags may be stale
after a separate resume. Stack creation alone is not deployment completion.

Report stage changes, errors, and observed success. Agree on an appropriate
update cadence within the harness's capabilities; do not hard-code stage
counts or expected durations. Use the release's expectations when explaining
progress. Investigate an overrun without treating elapsed time alone as failure.

Within one reply, check once (a few minutes at most), report, and say when
to check again; don't keep watching until something changes. Nothing runs
between replies unless the harness supports scheduled checks, so never
promise an update you can't send ("I'll report every 30 minutes"). If they
aren't supported, provide the
resolved README monitoring commands and configured notification path, and
explain how to return for a status check. Keep unverified completion pending.
Do not provision additional monitoring services to replace a harness capability.

Credential expiry affects observation; after the user refreshes and identity
is verified, continue monitoring without relaunching. On a failure, preserve
evidence and use [recover.md](recover.md).

## Verify and hand over

Verify the README's completion signal and the intended environment/host state
read-only. Distinguish tool completion from UI/application access you have
not tested.

Report outcome, account/Region, stack/environment IDs, release, log location,
access route, and continuing charges. For access or cleanup help, use
[operate.md](operate.md). Record completion evidence and stop monitoring
tasks/streams you started. A status request ends with the observed status.
