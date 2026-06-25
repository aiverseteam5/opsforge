---
process_key: incident-priority
observed_at: 2026-05-22
title: Priority Classification Policy
disposition_hint: prescriptive
---

# Priority Classification Policy

> REALISTIC ITIL TEST DATA — synthetic, not real-customer data. Authored to exercise the
> OpsForge reconciliation pipeline. Any finding from this content is a test finding.

## Scope and authority

This policy is the controlling authority for incident priority classification. It is mandatory.
Where operational practice diverges from this policy, the practice is a policy deviation and
must be corrected — **this document is the standard of record for priority assignment.**

## Roles

- **Compliance Owner** — maintains this policy and audits classification accuracy.
- **Incident Manager** — applies the policy when classifying incidents.

## P1 classification (strict)

A **P1 (Critical) classification strictly requires a complete service outage affecting all
users.** Partial degradation, single-tenant impact, or a workaround being available does NOT
meet the P1 bar and must be classified P2 or lower. The complete-outage, all-users condition is
the only valid basis for P1.

## Lower priorities

- **P2 (High)** — significant impact to a subset of users or a degraded but available service.
- **P3 (Medium)** — minor impact with a workaround; limited user scope.
- **P4 (Low)** — negligible or request-like, no service impact.

## Enforcement

Classifications are audited monthly against this policy. A P1 raised without a complete,
all-users outage is recorded as a classification deviation and reviewed with the team.
