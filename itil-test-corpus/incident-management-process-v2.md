---
process_key: incident-handling
observed_at: 2024-12-10
title: Incident Management Process v2
disposition_hint: descriptive
---

# Incident Management Process (v2)

> REALISTIC ITIL TEST DATA — synthetic, not real-customer data. Authored to exercise the
> OpsForge reconciliation pipeline. Any finding from this content is a test finding.

## Scope

This process governs the lifecycle of all IT service incidents from detection through
resolution and closure. It applies to the Service Operations function and all on-call
engineering teams. It is the authoritative incident-management reference for the organisation.

## Roles

- **Incident Manager** — owns the incident record, coordinates response, declares major incidents.
- **On-call Engineer (Tier 1)** — first responder; triages, classifies, and attempts resolution.
- **Service Owner** — accountable for the affected service; consulted on customer impact.
- **Major Incident Lead** — chairs the bridge for declared major incidents.

## Priority matrix

| Priority | Definition | Response target | Resolution target |
|----------|-----------|-----------------|-------------------|
| P1 | Critical business impact; major service degradation | 15 minutes | 4 hours |
| P2 | Significant impact; workaround may exist | 30 minutes | 8 hours |
| P3 | Minor impact; limited scope | 4 hours | 3 business days |
| P4 | Negligible impact; request-like | 1 business day | 5 business days |

## Service level targets

For a **P1 incident, the response target is 15 minutes and the resolution target is 4 hours.**
The resolution clock starts at the moment the incident is classified P1. Breach of the 4-hour
P1 resolution target requires a post-incident review.

## Escalation

If a P1 incident has not been acknowledged and actively worked **within 30 minutes** of
classification, it is automatically escalated to the on-call Incident Manager and the Service
Owner. **Escalation to on-call management occurs after 30 minutes** without progress. Further
escalation to engineering leadership occurs at the 2-hour mark if the P1 remains unresolved.

## Major incident bridge

A major-incident conference bridge is convened **for P1 incidents only.** P2 and lower
incidents are handled through the standard queue and do not warrant a bridge. The Major
Incident Lead chairs the P1 bridge and maintains the running timeline.

## Steps

1. Detect and record the incident; capture symptoms and affected service.
2. Classify priority using the priority matrix above.
3. For P1, start the response clock and notify the Incident Manager.
4. Investigate, identify a workaround or fix, and apply it.
5. Confirm service restoration with the Service Owner.
6. Close the incident and, for P1, schedule a post-incident review.
