"""M7.5/M7.6 — ticket-source behaviour: the pattern threshold + IDENTITY-BACKED origin.

Behaviour is the top of the trust ladder, sourced from a manipulable external system.
M7.5 set the property (a claim reaches behaviour-rank only as a genuine pattern of >= N
provenance-disjoint origins) but origin was attacker-controlled free text, so distinctness
was forgeable. M7.6 Job B binds origin to the connector-VERIFIED external identity (a real
directory id): the provenance root is the verified identity, set at ingest. Two origins are
distinct ONLY if their verified identities differ; an unverified/ambiguous/unavailable
identity → indeterminate root → demoted (research-rank, can't defeat the gate). This closes
the M7.5 residual at the root — forgery now requires a real directory identity.

Each test drives the real path: ingest tickets (with/without a verified identity) →
reconcile → assert the threshold/identity logic actually fired (per the M7.4/M7.5
discipline — no trivial passes on input the logic never evaluates).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from opsforge.dispositions import declare_disposition
from opsforge.ingest import hash_embedder
from opsforge.knowledge import (
    ProvenanceEnvelope,
    canonical_origin,
    get_chunks,
    provenance_root_for,
    store_chunk,
)
from opsforge.reconcile import ClaimRelation, FunctionDetector, reconcile_process
from opsforge.tickets import ingest_tickets, normalize_ticket

pytestmark = pytest.mark.usefixtures("db_required")

AS_OF = datetime(2026, 6, 21, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ticket(num, pk, origin, resolution, identity=None, resolved="2026-06-10"):
    """A resolved ticket. `identity` = the connector-VERIFIED directory id of the origin
    (None = unverified). In production the connector resolves it; here we pass it directly."""
    return {"number": num, "process_key": pk, "assignment_group": origin,
            "assignment_group_id": identity, "resolution": resolution,
            "resolved_at": f"{resolved}T00:00:00Z"}


async def _ingest(org, tickets):
    return await ingest_tickets(org, tickets, embedder=hash_embedder(), as_of=AS_OF)


async def _seed_doc(org, pk, content):
    env = ProvenanceEnvelope(
        source_kind="document", source_ref=f"doc://{pk}", observed_at=AS_OF, ingested_at=AS_OF
    )
    return await store_chunk(org_id=org, content=content, envelope=env, process_key=pk)


def _agree_all(behaviour_ids):
    want = set(behaviour_ids)

    async def fn(chunks):
        bids = [c.id for c in chunks if c.id in want]
        return [ClaimRelation(bids[i], bids[j], "agrees")
                for i in range(len(bids)) for j in range(i + 1, len(bids))]

    return FunctionDetector(fn)


def _pattern_vs_doc(behaviour_ids, doc_id):
    want = set(behaviour_ids)

    async def fn(chunks):
        present = {c.id for c in chunks}
        bids = [c.id for c in chunks if c.id in want]
        rels = [ClaimRelation(bids[i], bids[j], "agrees")
                for i in range(len(bids)) for j in range(i + 1, len(bids))]
        if bids and doc_id in present:
            rels.append(ClaimRelation(bids[0], doc_id, "contradicts"))
        return rels

    return FunctionDetector(fn)


async def _declare(org, pk, disposition="descriptive"):
    await declare_disposition(org_id=org, process_key=pk, disposition=disposition, rationale="t")


async def _cleanup(org):
    from opsforge.db import scope_to_org, session_factory

    async with session_factory().begin() as s:
        await scope_to_org(s, org)
        for t in ("reconciliations", "findings", "validated_processes",
                  "knowledge_chunks", "process_dispositions"):
            await s.execute(text(f"DELETE FROM {t} WHERE org_id=:o"), {"o": org})


async def _findings(org, pk):
    from opsforge.findings import list_findings

    return await list_findings(org, process_key=pk)


def _beh(chunks):
    return [c for c in chunks if c.source_kind == "behaviour"]


# --------------------------------------------------------------------------- #
# identity-backed provenance root (no DB)
# --------------------------------------------------------------------------- #
def test_canonical_origin_collapses_display_variants():
    assert canonical_origin("Sre-A") == "sre-a"
    assert canonical_origin("  sre-a ") == "sre-a"
    assert canonical_origin(" ") is None
    assert canonical_origin(None) is None


def test_provenance_root_is_identity_backed():
    # a ticket roots by its VERIFIED identity, not the free-text origin.
    assert provenance_root_for("sn://INC1", "sre-a", "grp-1") == "grp-1"
    assert provenance_root_for("sn://INC2", "sre-a", "grp-1") == "grp-1"   # same id → root
    assert provenance_root_for("sn://INC3", "sre-b", "grp-2") == "grp-2"   # distinct identity
    assert provenance_root_for("sn://INC4", "forged", None) is None  # unverified → indeterminate
    assert provenance_root_for("file://doc.md", None, None) == "file://doc.md"  # document


def test_normalize_extracts_identity_and_keeps_unverified():
    ok = normalize_ticket(_ticket("INC9", "p", "Sre-A", "did it", identity="grp-7"))
    assert ok is not None and ok["origin"] == "sre-a" and ok["origin_identity"] == "grp-7"
    # unverified identity → kept (stored but demoted), NOT dropped — still a real observation
    unv = normalize_ticket(_ticket("INC8", "p", "sre-a", "did it", identity=None))
    assert unv is not None and unv["origin_identity"] is None
    # but no display origin at all → dropped (can't attribute)
    assert normalize_ticket({"number": "X", "process_key": "p", "resolution": "did it"}) is None


# --------------------------------------------------------------------------- #
# §B4 — the identity adversarial tests (prove the M7.5 residual closes)
# --------------------------------------------------------------------------- #
async def test_verified_identity_distinctness_corroborates():
    """B4.1: two tickets from genuinely DISTINCT directory identities → distinct roots →
    they corroborate (a real pattern is recognized). Same identity → one root, no lift.
    (Don't over-correct into trusting nothing.)"""
    org = str(uuid.uuid4())
    try:
        distinct = await _ingest(org, [
            _ticket("D1", "p", "team-a", "did X", identity="grp-1"),
            _ticket("D2", "p", "team-b", "did X", identity="grp-2"),
            _ticket("D3", "p", "team-c", "did X", identity="grp-3"),
        ])
        await reconcile_process(org, "p", detector=_agree_all(distinct), as_of=AS_OF)
        assert all(c.corroborated_by == 2 for c in _beh(await get_chunks(org, "p")))  # 3 identities

        same = await _ingest(org, [_ticket(f"S{i}", "q", "team-a", "did X", identity="grp-1")
                                   for i in range(3)])
        await reconcile_process(org, "q", detector=_agree_all(same), as_of=AS_OF)
        assert all(c.corroborated_by == 0 for c in _beh(await get_chunks(org, "q")))  # one identity
    finally:
        await _cleanup(org)


async def test_m75_sockpuppet_attack_is_now_blocked():
    """B4.2: the EXACT M7.5 residual attack — mint distinct free-text origins and launder
    them via multi-process breadth — is now BLOCKED, because none has a verified identity.
    40 distinct origins (job-0..job-39) with NO directory identity, even with cross-process
    'attestation' tickets, all root to None → zero corroboration, support 0 → demoted."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        # the M7.5 laundering: each forged origin also appears on a junk process
        for i in range(40):
            await _ingest(org, [_ticket(f"J{i}", f"junk-{i}", f"job-{i}", "filler", identity=None)])
        ids = await _ingest(org, [
            _ticket(f"INC{i}", pk, f"job-{i}", "fabricated", identity=None) for i in range(40)
        ])
        doc = await _seed_doc(org, pk, "the real documented rollback")
        await _declare(org, pk, "descriptive")
        res = await reconcile_process(org, pk, detector=_pattern_vs_doc(ids, doc), as_of=AS_OF)

        assert res.findings_by_kind.get("drift", 0) == 0  # no verified identity → no pattern
        assert all(c.corroborated_by == 0 for c in _beh(await get_chunks(org, pk)))  # zero lift
    finally:
        await _cleanup(org)


async def test_identity_unavailable_fails_safe():
    """B4.3: a ticket whose origin identity can't be resolved → demoted to research-rank,
    does NOT reach behaviour-rank or defeat the M6.5 gate."""
    from opsforge.agent import assemble_context
    from opsforge.policy import resolve_proposal

    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _ingest(org, [_ticket("INC1", pk, "sre-a", "restart everything now", identity=None)])
        await reconcile_process(org, pk, detector=_agree_all([]), as_of=AS_OF)
        row = _beh(await get_chunks(org, pk))[0]
        assert row.confidence is not None and float(row.confidence) < 0.5  # demoted, not 0.65

        _ctx, grounding = await assemble_context(
            org, {"context": {"graph": False}}, "i", {"query": "q", "process_key": pk}, []
        )
        assert grounding["low_confidence"] is True
        trace = resolve_proposal(
            {"proposals": [{"tool": "k.restart", "class": "reversible"}]},
            "k.restart", {"k.restart": "auto_with_notify"}, grounding=grounding,
        )
        assert trace["state"] == "awaiting_approval"
        assert "low_grounding_gate" in trace["rules"]
    finally:
        await _cleanup(org)


async def test_identity_spoof_is_not_a_verified_root():
    """B4.4: an attacker-supplied free-text origin that does not map to a real directory
    identity (identity None) is not counted as a verified distinct root, even paired with a
    genuinely-verified one — a forged 'second origin' can't manufacture a 2-identity pattern."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        ids = await _ingest(org, [
            _ticket("REAL", pk, "sre-a", "rollback the new way", identity="grp-1"),  # verified
            _ticket("SPOOF", pk, "fake-team", "rollback the new way", identity=None),  # spoof
        ])
        doc = await _seed_doc(org, pk, "rollback must be done the old way")
        await _declare(org, pk, "descriptive")
        res = await reconcile_process(org, pk, detector=_pattern_vs_doc(ids, doc), as_of=AS_OF)
        # only ONE verified identity → support 1 < 2 → demoted, document not overridden
        assert res.findings_by_kind.get("drift", 0) == 0
        f = next(x for x in await _findings(org, pk) if x.kind == "contradiction")
        assert f.detail["action"] == "behaviour_below_pattern_threshold"
        assert f.detail["distinct_origins"] == 1
    finally:
        await _cleanup(org)


async def test_unverified_center_cannot_borrow_verified_partners():
    """B4.5 (the review's HIGH — close, don't relocate): an UNVERIFIED ticket must not reach
    behaviour-rank by BORROWING the verified identities of partners it agrees with. One
    identity=None ticket agreeing with two genuinely-verified ones earns ZERO support → it
    stays research-rank (< gate) and does NOT override the document it contradicts."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        ids = await _ingest(org, [
            _ticket("V1", pk, "team-a", "drain node redeploy prior image", identity="g1"),
            _ticket("V2", pk, "team-b", "drain node redeploy prior image", identity="g2"),
            _ticket("SPOOF", pk, "fake", "drain node redeploy prior image", identity=None),
        ])
        doc = await _seed_doc(org, pk, "rollback restores from backup")
        await _declare(org, pk, "descriptive")
        spoof = ids[2]

        async def det(chunks):
            present = {c.id for c in chunks}
            bids = [c.id for c in chunks if c.id in set(ids)]
            rels = [ClaimRelation(bids[i], bids[j], "agrees")
                    for i in range(len(bids)) for j in range(i + 1, len(bids))]
            if doc in present:
                rels.append(ClaimRelation(spoof, doc, "contradicts"))
            return rels

        res = await reconcile_process(org, pk, detector=FunctionDetector(det), as_of=AS_OF)
        assert res.findings_by_kind.get("drift", 0) == 0  # the unverified spoof does NOT override
        by = {c.id: c for c in _beh(await get_chunks(org, pk))}
        assert float(by[spoof].confidence) < 0.5  # borrowed nothing → research rank, not 0.65
    finally:
        await _cleanup(org)


async def test_self_asserted_origin_identity_is_not_verified():
    """B4.6 (review HIGH F-A — the fail-OPEN hole): a raw ticket whose group the connector
    could NOT resolve (no assignment_group_id) must not mint a VERIFIED root by self-asserting
    an `origin_identity` field on the record. Only the connector-stamped assignment_group_id
    counts; the attacker-injected field is ignored → root None → demoted. Two such tickets
    cannot manufacture a 2-identity pattern, so the document is not overridden."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        # Raw connector payloads with a display origin but NO assignment_group_id (the
        # directory did not resolve the group) — plus an attacker-injected origin_identity.
        raw = [
            {"number": "ATK1", "process_key": pk, "assignment_group": "ghost-a",
             "origin_identity": "x1", "resolution": "drain node redeploy prior image",
             "resolved_at": "2026-06-10T00:00:00Z"},
            {"number": "ATK2", "process_key": pk, "assignment_group": "ghost-b",
             "origin_identity": "x2", "resolution": "drain node redeploy prior image",
             "resolved_at": "2026-06-10T00:00:00Z"},
        ]
        ids = await ingest_tickets(org, raw, embedder=hash_embedder(), as_of=AS_OF)
        # the self-asserted identity was NOT honoured → provenance_root stays None
        assert all(r.provenance_root is None for r in _beh(await get_chunks(org, pk)))

        doc = await _seed_doc(org, pk, "rollback restores from backup")
        await _declare(org, pk, "descriptive")
        res = await reconcile_process(org, pk, detector=_pattern_vs_doc(ids, doc), as_of=AS_OF)
        assert res.findings_by_kind.get("drift", 0) == 0  # no verified pattern → no override
        assert all(float(r.confidence) < 0.5 for r in _beh(await get_chunks(org, pk)))  # demoted
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# the pattern-threshold tests (now over verified identities)
# --------------------------------------------------------------------------- #
async def test_single_event_does_not_reach_behaviour_rank():
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        ids = await _ingest(
            org, [_ticket("INC1", pk, "sre-a", "restore from backup", identity="grp-1")]
        )
        doc = await _seed_doc(org, pk, "rollback drains the node and redeploys")
        await _declare(org, pk, "descriptive")
        res = await reconcile_process(org, pk, detector=_pattern_vs_doc(ids, doc), as_of=AS_OF)
        assert res.findings_by_kind.get("drift", 0) == 0
        f = next(x for x in await _findings(org, pk) if x.kind == "contradiction")
        assert f.detail["action"] == "behaviour_below_pattern_threshold"
        assert f.detail["distinct_origins"] == 1
    finally:
        await _cleanup(org)


async def test_single_identity_volume_is_not_a_pattern():
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        tix = [
            _ticket(f"INC{i}", pk, "auto-job", "auto rollback ran", identity="grp-99")
            for i in range(40)
        ]
        ids = await _ingest(org, tix)
        doc = await _seed_doc(org, pk, "rollback restores from last night's backup")
        await _declare(org, pk, "descriptive")
        res = await reconcile_process(org, pk, detector=_pattern_vs_doc(ids, doc), as_of=AS_OF)
        assert res.findings_by_kind.get("drift", 0) == 0
        f = next(x for x in await _findings(org, pk) if x.kind == "contradiction")
        assert f.detail["distinct_origins"] == 1  # 40 tickets, one verified identity
    finally:
        await _cleanup(org)


async def test_genuine_multi_identity_pattern_reaches_behaviour_rank():
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        ids = await _ingest(org, [
            _ticket("INC1", pk, "sre-payments", "drain node, redeploy prior image", identity="g1"),
            _ticket("INC2", pk, "sre-checkout", "drained then redeployed", identity="g2"),
            _ticket("INC3", pk, "platform-oncall", "drain node, redeploy old image", identity="g3"),
        ])
        doc = await _seed_doc(org, pk, "rollback means restoring from last night's backup")
        await _declare(org, pk, "descriptive")
        res = await reconcile_process(org, pk, detector=_pattern_vs_doc(ids, doc), as_of=AS_OF)
        assert res.findings_by_kind.get("drift", 0) == 1
        assert res.findings_by_kind.get("contradiction", 0) == 0
        f = next(x for x in await _findings(org, pk) if x.kind == "drift")
        assert f.detail["action"] == "update_document_to_match_behaviour"
    finally:
        await _cleanup(org)


async def test_fresh_single_ticket_is_demoted_below_the_gate():
    from opsforge.agent import assemble_context
    from opsforge.policy import resolve_proposal

    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _ingest(org, [_ticket("INC1", pk, "sre-a", "restart now", identity="grp-1")])
        await reconcile_process(org, pk, detector=_agree_all([]), as_of=AS_OF)
        row = _beh(await get_chunks(org, pk))[0]
        assert float(row.confidence) < 0.5  # one identity < threshold → research rank, not 0.65

        _ctx, grounding = await assemble_context(
            org, {"context": {"graph": False}}, "i", {"query": "q", "process_key": pk}, []
        )
        assert grounding["low_confidence"] is True
        trace = resolve_proposal(
            {"proposals": [{"tool": "k.restart", "class": "reversible"}]},
            "k.restart", {"k.restart": "auto_with_notify"}, grounding=grounding,
        )
        assert trace["state"] == "awaiting_approval"
    finally:
        await _cleanup(org)


async def test_document_corroborator_is_not_a_second_origin():
    """One verified ticket origin must not borrow a DOCUMENT agreer as its 'second origin'
    to reach pattern rank and override another document."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        await _ingest(
            org, [_ticket("INC1", pk, "sre-a", "rollback the new way", identity="grp-1")]
        )
        doc_a = await _seed_doc(org, pk, "doc A: rollback is done the new way")
        doc_b = await _seed_doc(org, pk, "doc B: rollback must be done the old way")
        await _declare(org, pk, "descriptive")

        async def det(chunks):
            t = next(c.id for c in chunks if c.source_kind == "behaviour")
            return [ClaimRelation(t, doc_a, "agrees"), ClaimRelation(t, doc_b, "contradicts")]

        res = await reconcile_process(org, pk, detector=FunctionDetector(det), as_of=AS_OF)
        assert res.findings_by_kind.get("drift", 0) == 0
        assert float(_beh(await get_chunks(org, pk))[0].confidence) < 0.5
    finally:
        await _cleanup(org)


async def test_human_asserted_behaviour_is_not_identity_gated():
    """Origin-less (human-asserted) behaviour keeps its rank — a human is its own vouching
    origin (this is why the M7.3 harness still HOLDS)."""
    org, pk = str(uuid.uuid4()), "deploy"
    try:
        env = ProvenanceEnvelope(
            source_kind="behaviour", source_ref="human://op", observed_at=AS_OF, ingested_at=AS_OF
        )
        beh = await store_chunk(
            org_id=org, content="we drain the node", envelope=env, process_key=pk
        )
        doc = await _seed_doc(org, pk, "rollback restores from backup")
        await _declare(org, pk, "descriptive")
        res = await reconcile_process(org, pk, detector=_pattern_vs_doc([beh], doc), as_of=AS_OF)
        assert res.findings_by_kind.get("drift", 0) == 1
    finally:
        await _cleanup(org)


# --------------------------------------------------------------------------- #
# §1 — ingestion through the connector path (now resolves verified identity)
# --------------------------------------------------------------------------- #
async def test_ingest_through_connector_resolves_verified_identity():
    import json

    from fake_mcp import server_command

    from opsforge.connectors import load_connector
    from opsforge.db import scope_to_org, session_factory
    from opsforge.tickets import ingest_tickets_from_connector

    org = str(uuid.uuid4())
    try:
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            cid = (
                await s.execute(
                    text(
                        "INSERT INTO connectors (org_id, name, kind, transport, endpoint, "
                        "tool_allowlist, status) VALUES "
                        "(:o, 'snow', 'servicenow', 'stdio', :e, CAST(:a AS jsonb), 'healthy') "
                        "RETURNING id"
                    ),
                    {"o": org, "e": server_command("servicenow"),
                     "a": json.dumps(["list_resolved_incidents"])},
                )
            ).scalar_one()
        connector = await load_connector(cid, org)
        await ingest_tickets_from_connector(connector, org_id=org, embedder=hash_embedder())

        rollback = _beh(await get_chunks(org, "deploy-rollback"))
        # origins resolved to DISTINCT verified directory identities (sys_ids), not free text
        assert {c.provenance_root for c in rollback} == {
            "grp-sys-0001", "grp-sys-0002", "grp-sys-0003"
        }
    finally:
        async with session_factory().begin() as s:
            await scope_to_org(s, org)
            await s.execute(text("DELETE FROM connectors WHERE org_id=:o"), {"o": org})
        await _cleanup(org)
