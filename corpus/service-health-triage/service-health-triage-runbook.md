---
process_key: service-health-triage
observed_at: 2026-05-20
title: Service Health Triage Runbook
disposition_hint: descriptive
---

# Service Health Triage Runbook

> REALISTIC TEST DATA — synthetic operations content authored to exercise the OpsForge
> learn-the-operation + validate-the-signal loop. NOT real-customer data; any finding or
> validated process derived from it is test output.

## Scope

This runbook governs first-line triage of "service down" / "service unhealthy" tickets for the
platform on-call team. Its purpose is to determine whether the reported problem is REAL before
any remediation, and to catch false alerts caused by stale monitoring data.

## Roles

- **On-call Engineer** — runs the triage, owns the ticket until resolved or escalated.
- **Service Owner** — consulted when the incident is confirmed real and customer-impacting.

## Triage procedure

1. **Read the ticket claim.** Note the affected service and the reported symptom (e.g. "down",
   "5xx", "unreachable") and the time the alert fired.
2. **Check the monitoring system for the service's current status.** Query the monitoring tool
   for the service's live health (status, last-check time). This is the ground-truth read — do
   not take the ticket's claim at face value.
3. **Compare monitoring to the ticket claim — and check whether the DATA is stale.** If monitoring
   reports the service UP and healthy while the ticket says it is DOWN, check whether the monitoring
   data itself is stale — the data-pull lagging past its refresh interval (e.g. a `data_stale` flag
   or a non-zero lag). UP-but-STALE data while the ticket says DOWN is the stale / false-alert
   signature: the alert reflects old data, not current reality. If monitoring is UP with FRESH data
   (not stale), there is no lagging data-pull and this is NOT the stale-alert case.
4. **For a suspected stale/false alert, the remediation is to fix the data-pull, not the
   service.** Propose tightening the monitoring data source's pull/refresh interval so the
   dashboard reflects current reality and the false alert clears. Do not restart or touch the
   (healthy) service.
5. **Surface the report-vs-reality discrepancy to a human and wait for approval.** State plainly
   what monitoring shows versus what the ticket claims, mark the conflict as unresolved/contested,
   and propose exactly one fix for the human to approve. Never close the ticket or apply a change
   autonomously.
6. **After the fix is approved and applied, VERIFY it worked — re-read ground truth.** Once the
   data-pull change has been applied, query the monitoring tool for the service's status AGAIN and
   check whether the staleness cleared (the data-pull is back within its refresh window and the
   reading is fresh). Do not assume the fix worked from the change's own result alone — confirm it
   against the live system.
7. **Close out, or take the next gated step.** If monitoring is now healthy AND no longer stale,
   the stale alert is resolved — record the before/after readings and close the triage. If it is
   still stale, the first adjustment was insufficient: tighten the pull interval further (one more
   gated step, same approval path) or, if repeated adjustments do not help, escalate to the Service
   Owner. Never declare the alert resolved without a fresh ground-truth read confirming it.

## Notes

- A confirmed-real outage (monitoring also shows the service down) follows the standard incident
  path and is out of scope for this stale-alert runbook.
- The stale-alert pattern is common when a monitoring data source's pull interval is set too
  long relative to the alert evaluation window.
