# ITIL Test Corpus — REALISTIC TEST DATA (synthetic)

> **This is a REALISTIC ITIL TEST CORPUS — synthetic, deliberately-contradictory content
> authored to exercise the OpsForge ingest → reconcile → chat loop. It is NOT real-customer
> data and any finding produced from it is NOT a real-customer finding. The pipeline run is
> real; the data is synthetic-but-realistic.**

Five enterprise-style ITIL process documents with realistic structure (scope, roles, priority
matrix, SLAs, escalation, steps) and realistic `observed_at` dates spread across ~18 months, so
freshness/staleness is exercised. Deliberate, true-to-life contradictions are baked in:

| File | observed_at | process_key | role |
|------|-------------|-------------|------|
| incident-management-process-v2.md | 2024-12-10 (~18mo) | incident-handling | "official" process: P1 4-hr resolution, escalate @30min, bridge P1-only |
| incident-management-runbook-2026.md | 2026-04-22 (~2mo) | incident-handling | newer runbook: P1 **2-hr** resolution, escalate **@15min**, bridge **P1+P2** (contradicts) |
| escalation-matrix.md | 2025-12-20 (~6mo) | incident-handling | references the **30-min** trigger (agrees with the process, disagrees with the runbook) |
| priority-classification-policy.md | 2026-05-22 (~1mo) | incident-priority | PRESCRIPTIVE policy: strict P1 = total outage |
| change-management-process.md | 2025-06-18 (~12mo) | change-management | self-consistent control doc (should yield NO finding) |

Designed signals: the incident process↔runbook drift (resolution target, escalation timing,
bridge scope); a staleness signal (18-month process vs 2-month runbook); a 3-way escalation
reconciliation (process + matrix @30min vs runbook @15min); and NO finding on the clean
change-management doc.
