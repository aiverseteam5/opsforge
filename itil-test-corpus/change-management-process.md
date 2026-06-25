---
process_key: change-management
observed_at: 2025-06-18
title: Change Management Process
disposition_hint: descriptive
---

# Change Management Process

> REALISTIC ITIL TEST DATA — synthetic, not real-customer data. Authored to exercise the
> OpsForge reconciliation pipeline. This document is internally self-consistent and is the
> control case: a clean process that should produce NO reconciliation finding.

## Scope

This process governs all changes to production services: standard, normal, and emergency
changes. It applies to all engineering teams deploying to production.

## Roles

- **Change Requester** — raises the change and provides the implementation and rollback plan.
- **Change Manager** — schedules changes and runs the Change Advisory Board (CAB).
- **CAB** — reviews and authorises normal and emergency changes.

## Change types

| Type | Definition | Approval |
|------|-----------|----------|
| Standard | Pre-approved, low-risk, repeatable | Pre-authorised; no CAB needed |
| Normal | Non-routine; requires assessment | CAB approval required |
| Emergency | Needed to restore service or prevent imminent impact | Emergency CAB approval |

## Approval

- **Standard changes** are pre-authorised against an approved template and do not require CAB.
- **Normal changes** require CAB approval before scheduling.
- **Emergency changes** require Emergency CAB approval, which may be obtained out of band and
  ratified at the next CAB.

## Change windows

Normal changes are scheduled into the weekly change window (Wednesday 22:00–02:00). Emergency
changes may proceed outside the window with Emergency CAB approval. Standard changes may be
executed at any time per their approved template.

## Steps

1. Raise the change with type, implementation plan, and rollback plan.
2. For normal/emergency changes, obtain the appropriate CAB approval.
3. Schedule into the change window (or out of band for emergencies).
4. Implement, verify, and record the outcome.
5. Close the change; on failure, execute the rollback plan.
