---
process_key: incident-handling
observed_at: 2026-04-22
title: Incident Management Runbook 2026
disposition_hint: descriptive
---

# Incident Management Runbook (2026)

> REALISTIC ITIL TEST DATA — synthetic, not real-customer data. Authored to exercise the
> OpsForge reconciliation pipeline. Any finding from this content is a test finding.

## Scope

This runbook is the current operational guide for the on-call team handling live incidents in
2026. It reflects how incidents are actually run today following the 2026 SLA tightening. It
supersedes older guidance where the two differ.

## Roles

- **On-call Engineer** — first responder and incident owner until handed off.
- **Incident Commander** — coordinates major incidents and the bridge.
- **Comms Lead** — owns stakeholder and customer communication during major incidents.

## Service level targets

After the 2026 SLA tightening, **a P1 incident must be resolved within 2 hours.** The P1
response target remains 15 minutes, but **the P1 resolution target is now 2 hours**, not the
older four-hour window. The 2-hour P1 resolution clock starts at classification and is tracked
on the incident dashboard.

## Escalation

Escalation is faster under the 2026 runbook. **If a P1 is not actively being worked within 15
minutes, escalate immediately** to the Incident Commander. **Escalation to on-call management
occurs after 15 minutes** without active progress — do not wait longer. A second escalation to
engineering leadership follows at 45 minutes if unresolved.

## Major incident bridge

Convene a conference bridge for **both P1 and P2 incidents.** Under current practice a P2 with
customer impact also warrants a bridge so comms and engineering stay aligned; **the bridge is
no longer P1-only.** The Incident Commander runs the bridge and the Comms Lead handles updates.

## Steps

1. Acknowledge the alert and open the incident record.
2. Classify priority; for P1 or P2 with customer impact, spin up the bridge.
3. Start the resolution clock (P1: 2 hours) and post to the incident channel.
4. Drive investigation, mitigate, and verify recovery.
5. Hand off or close, then capture timeline notes for the review.
