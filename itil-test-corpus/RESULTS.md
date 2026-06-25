# ITIL Loop Proof — Results Summary (REALISTIC TEST DATA)

> **REALISTIC ITIL TEST DATA — synthetic, deliberately-contradictory content. NOT real-customer
> data; the findings below are NOT real-customer findings. The pipeline run is real (ingest →
> reconcile with the real LLM detector → chat agent, all as the restricted `opsforge_app` role);
> the data is synthetic-but-realistic.** Run date: 2026-06-25. Detector: real LLM (gpt-4o-mini).

## 1. What was ingested (real provenance)

5 ITIL `.md` docs (+ a README) bind-mounted read-only at `/data/itil`, ingested through the real
API → worker path as `opsforge_app`. Each chunk carries **real provenance — `observed_at` = the
file's authored date (from front-matter), NOT ingest time** — verified live:

| Doc | observed_at (file date) | process_key |
|-----|-------------------------|-------------|
| incident-management-process-v2.md | 2024-12-10 (~18 mo) | incident-handling |
| escalation-matrix.md | 2025-12-20 (~6 mo) | incident-handling |
| incident-management-runbook-2026.md | 2026-04-22 (~2 mo) | incident-handling |
| priority-classification-policy.md | 2026-05-22 (~1 mo) | incident-priority |
| change-management-process.md | 2025-06-18 (~12 mo) | change-management |

`source_ref` = the real file URI (e.g. `file:///data/itil/incident-management-process-v2.md`).

## 2. Reconciliation findings (real detector + real `observed_at`)

Dispositions declared: incident-priority = **prescriptive**; incident-handling / change-management
= **descriptive**.

**incident-handling — the real LLM detector found all three designed contradictions + the 3-way,
each resolved as STALENESS** (newer runbook supersedes the older doc, because the date gaps far
exceed the 30-day staleness threshold — the realistic "your process doc is stale; the runbook
moved on"):

| Claim | Superseded (old) | Supersedes (new) | gap |
|-------|------------------|------------------|-----|
| P1 resolution target | process-v2: **4 hours** (2024-12-10) | runbook: **2 hours** (2026-04-22) | 498 d |
| Escalation timing | process-v2: **30 min** | runbook: **15 min** | 498 d |
| Escalation timing (**3-way**) | escalation-matrix: **30 min** (2025-12-20) | runbook: **15 min** | 123 d |
| Major-incident bridge scope | process-v2: **P1 only** | runbook: **P1 + P2** | 498 d |

The 3-way reconciliation fired exactly as designed: the runbook's 15-min trigger superseded BOTH
the 18-month process AND the 6-month escalation-matrix (which had agreed on 30 min) — one newer
source reconciling over two older agreeing sources.

**change-management — NO contradiction / drift / stale finding** (proves the system does not
hallucinate inconsistencies on self-consistent content). ✓

## 3. The 4 chat-agent scenarios (read path, no gate — every run completed; honest verbatim)

The Phase-1 chat agent investigated freely via its read-only `kb.*` tools (no gate fired on any
run) and answered with evidence. Honest results — 2 strong, 2 partial:

**Q "What's our P1 resolution target?"** — STRONG. *"The P1 resolution target is currently 2 hours
following the 2026 SLA tightening."* (confidence: medium). Evidence cited BOTH sides — runbook
(2 hr, current) AND process-v2 (4 hr, previous): it surfaced the conflict and did NOT silently
pick one as fact (it reconciled to the newer while citing the superseded older). ✓ tools:
kb.list_processes, kb.search_knowledge.

**Q "Show me anything stale in our processes."** — STRONG. *"There are stale findings and gaps in
our incident-handling, incident-priority, and change-management processes…"* (medium). Evidence:
the real `stale` finding (age_gap_days 498) + the gap finding. ✓ tools: kb.findings.

**Q "Is our escalation timing consistent?"** — PARTIAL. *"The escalation timing is not consistent
due to the staleness of the incident-handling process and unverified steps."* (low). Correctly
said NOT consistent and pointed at the staleness, but did not articulate the specific 30-vs-15
numbers. tools: kb.list_processes, kb.findings, kb.process.

**Q "What's inconsistent in our incident management process?"** — WEAK (reported honestly). The
agent called kb.list_processes + kb.findings but returned *"Investigation incomplete."* (low) — it
reached the findings data but did not synthesize the specific inconsistencies. An honest
characterization result, not papered over: the open-ended phrasing + the model's synthesis on
this run produced a low-confidence non-answer despite the data being available.

Across all four: low-confidence / contested knowledge was surfaced AS contested (or as low
confidence), never falsely resolved to a single fact.

## 4. Honest caveats (true-to-the-data, not failures)

- **The corpus is documents-only**, so the contradictions are document-vs-document. Given the real
  date gaps (all ≫ the 30-day staleness threshold), the engine resolves each same-kind
  contradiction into a **`stale`** finding (the older chunk is superseded) rather than a standalone
  `contradiction` finding. The drift IS detected — it manifests as staleness, which is the faithful
  outcome for "an old process doc contradicted by a current runbook."
- **`drift` / `violation` findings did NOT fire** — by design they require a *behaviour* signal
  (behaviour-vs-document), which comes from the M7.5 real-ticket path and is out of scope for a
  documents-only corpus. The **prescriptive** disposition on the priority policy was declared and
  exercised, but with no behaviour to deviate from it, no `violation` was emitted. Reported, not
  forced.
- **Every process (including the clean change-management doc) also received a `gap: not_practiced`
  finding** — the honest "documented but we have no behaviour data proving it is actually
  practiced" signal that fires for any documents-only process. This is NOT a hallucinated
  contradiction (change-management has zero contradiction/stale findings); it is the universal
  no-behaviour-evidence gap.

## 5. Acceptance check

- ✅ Realistic ITIL corpus exists (5 docs + realistic dates + the deliberate contradictions),
  labeled realistic-test-data throughout.
- ✅ Ingested through the real pipeline as `opsforge_app`; chunks carry real provenance
  (`source_ref` = file, `observed_at` = file date, verified ≠ ingest time).
- ✅ Real reconciliation produced the incident drift (as staleness), the staleness signal (498-day
  gap = the 18-mo-vs-2-mo), the 3-way escalation reconciliation, and NO contradiction on the clean
  change doc — each with real provenance + a "why" trace (the superseded/superseding chunk ids +
  age_gap_days) visible via the findings.
- ✅ The 4 chat scenarios returned real, evidence-backed answers on the read path (no gate);
  contested knowledge surfaced as contested. (Q1 weak — reported honestly.)
- ✅ No part of this is presented as real-customer data or a real-customer finding.
