---
process_key: incident-handling
observed_at: 2025-12-20
title: Incident Escalation Matrix
disposition_hint: descriptive
---

# Incident Escalation Matrix

> REALISTIC ITIL TEST DATA — synthetic, not real-customer data. Authored to exercise the
> OpsForge reconciliation pipeline. Any finding from this content is a test finding.

## Scope

This matrix defines the tiered escalation path for incidents and the time triggers at each
tier. It is referenced by the incident management process and used by the on-call rota.

## Escalation tiers

| Tier | Owner | Engaged when |
|------|-------|--------------|
| Tier 1 | On-call Engineer | At incident classification |
| Tier 2 | Incident Manager | Per the time trigger below |
| Tier 3 | Engineering Leadership | If unresolved at 2 hours |
| Tier 4 | Executive / Major Incident | On declaration of a major incident |

## Time trigger

For a P1 incident, **the Tier-2 escalation trigger is 30 minutes.** If a P1 has not been
actively progressed **within 30 minutes** of classification, the Incident Manager is engaged.
This 30-minute trigger aligns with the incident management process and is the standard the rota
is measured against.

## Notification

- Tier 2 is engaged by page and by the incident channel.
- Tier 3 and Tier 4 are engaged by the Incident Manager once the relevant trigger is met.
- The Service Owner is informed at Tier 2 for any customer-facing service.
