"""Ingest pipeline: normalize → chunk → embed → store with a provenance envelope.

M6.1 proves the model on local Markdown (no auth). Source breadth
(SharePoint/Confluence/PST) is M7; the envelope is built so new sources are
configuration, not schema change. The embedder is injected so the pipeline and
envelope are fully testable without a live embedding call (it is the last,
mockable stage). `observed_at` comes from front-matter (when the knowledge was
*written*), falling back to file mtime — never ingest time.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from .config import get_settings
from .knowledge import PendingChunk, ProvenanceEnvelope, store_chunks
from .models import KnowledgeSourceKind
from .security import redact

# An embedder maps a batch of texts to a batch of vectors.
Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]

_MAX_CHUNK_CHARS = 1200
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s+\S")


@dataclass(frozen=True)
class Chunk:
    text: str
    heading: str | None


def parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """Split optional YAML front-matter from the markdown body."""
    m = _FRONT_MATTER.match(raw)
    if not m:
        return {}, raw
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, raw[m.end() :]


def _coerce_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        try:
            d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    return None


def extract_observed_at(front_matter: dict[str, Any], path: Path) -> datetime:
    """When the knowledge was true/written: a front-matter date if present, else
    the file's mtime. Never the ingest time (doctrine: observed_at ≠ ingested_at)."""
    for key in ("observed_at", "updated", "date"):
        if key in front_matter:
            dt = _coerce_dt(front_matter[key])
            if dt is not None:
                return dt
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _pack(text: str, max_chars: int) -> list[str]:
    """Pack paragraphs into ≤max_chars pieces; hard-split any oversized paragraph."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > max_chars:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
        while len(cur) > max_chars:
            out.append(cur[:max_chars])
            cur = cur[max_chars:]
    if cur.strip():
        out.append(cur.strip())
    return out


def chunk_markdown(body: str, *, max_chars: int = _MAX_CHUNK_CHARS) -> list[Chunk]:
    """Heading-aware chunker: break on markdown headings, then pack paragraphs to
    a size bound, prefixing each chunk with its heading for retrieval context.
    A heading with no body still yields a chunk (so title-only or all-heading
    files are never silently dropped — doctrine #2). The heading prefix is
    counted against `max_chars` so the bound holds."""
    chunks: list[Chunk] = []
    heading: str | None = None
    heading_used = False
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, heading_used
        section = "\n".join(buf).strip()
        buf = []
        if section:
            prefix = f"{heading}\n\n" if heading else ""
            budget = max(1, max_chars - len(prefix))
            for piece in _pack(section, budget):
                chunks.append(Chunk(text=f"{prefix}{piece}".strip(), heading=heading))
            heading_used = True
        elif heading is not None and not heading_used:
            # A heading with no body is still a fact worth keeping.
            chunks.append(Chunk(text=heading, heading=heading))
            heading_used = True

    for line in body.splitlines():
        if _HEADING.match(line):
            flush()
            heading = line.strip()
            heading_used = False
        else:
            buf.append(line)
    flush()
    return chunks


def hash_embedder() -> Embedder:
    """Deterministic, keyless embedder for dev/demo when no LLM provider is
    configured. Produces a stable 1536-vector from a content hash — fine for
    storage and round-tripping; real semantic embeddings are the gateway path
    (default_embedder) and need a provider key. Not semantically meaningful."""
    import hashlib

    async def _embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([(h[i % len(h)] / 255.0) - 0.5 for i in range(1536)])
        return out

    return _embed


def default_embedder() -> Embedder:
    """Production embedder: the configured embedding model via the gateway. Needs
    the provider key in the environment (OPENAI_API_KEY for text-embedding-3-*)."""
    from .gateway import LiteLLMGateway

    gw = LiteLLMGateway()
    model = get_settings().embedding_model

    async def _embed(texts: list[str]) -> list[list[float]]:
        return await gw.embedding(texts, model)

    return _embed


def configured_embedder() -> Embedder:
    """Real gateway embedder when a provider key is configured, else the keyless
    hash stand-in — so ingest works with or without an LLM provider."""
    import os

    if os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return default_embedder()
    return hash_embedder()


async def ingest_markdown_file(
    path: Path,
    *,
    org_id: Any,
    embedder: Embedder,
    process_key: str | None = None,
    source_kind: KnowledgeSourceKind = "document",
) -> list[UUID]:
    """Chunk, embed, and store one markdown file with its provenance envelope.
    `process_key` is provisional (front-matter may set it); clustering finalizes
    it in M6.3. Returns the stored chunk ids."""
    raw = path.read_text(encoding="utf-8")
    front_matter, body = parse_front_matter(raw)

    observed_at = extract_observed_at(front_matter, path)
    ingested_at = datetime.now(UTC)
    # A markdown file is a declared *document*; its front-matter may demote it to
    # research but must NOT let it self-assert observed behaviour — rank must stay
    # trustworthy data (doctrine #3). The caller (trusted code) may still pass a
    # behaviour source_kind for a genuine behaviour source.
    fm_kind = front_matter.get("source_kind")
    kind: KnowledgeSourceKind = fm_kind if fm_kind in ("document", "research") else source_kind
    source_ref = front_matter.get("source_ref") or f"file://{path.resolve().as_posix()}"
    pk = process_key or front_matter.get("process_key")

    chunks = chunk_markdown(body)
    if not chunks:
        return []

    # Redact credential-shaped substrings before content crosses a boundary: it
    # is sent to a third-party embedder and persisted into knowledge_chunks.content
    # (which feeds M6.5 context-assembly). redact() is pure and idempotent, so
    # ordinary prose is untouched.
    safe_texts = [redact(c.text) for c in chunks]
    vectors = await embedder(safe_texts)
    if len(vectors) != len(chunks):
        raise ValueError(
            f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
        )

    # Build every envelope first (an invalid source_kind raises before any store),
    # then write the whole file in one transaction (atomic per document).
    pending = [
        PendingChunk(
            content=safe_texts[i],
            envelope=ProvenanceEnvelope(
                source_kind=kind,
                source_ref=source_ref,
                observed_at=observed_at,
                ingested_at=ingested_at,
            ),
            embedding=vectors[i],
            process_key=pk,
        )
        for i in range(len(chunks))
    ]
    return await store_chunks(org_id, pending)


async def ingest_directory(
    folder: str | Path,
    *,
    org_id: Any,
    embedder: Embedder | None = None,
    source_kind: KnowledgeSourceKind = "document",
) -> dict[str, Any]:
    """Ingest every `*.md` under a folder. Returns a summary {files, chunks}.

    The resolved path must fall within OPSFORGE_KNOWLEDGE_BASE_PATH; requests
    outside that root are rejected to prevent filesystem traversal by operators.
    """
    allowed_root = Path(get_settings().knowledge_base_path).resolve()
    root = Path(folder).resolve()
    if not str(root).startswith(str(allowed_root) + "/") and root != allowed_root:
        raise ValueError(
            f"ingest path {root} is outside the allowed knowledge root {allowed_root}"
        )
    embed = embedder or default_embedder()
    files = sorted(root.rglob("*.md"))
    all_ids: list[UUID] = []
    for path in files:
        ids = await ingest_markdown_file(
            path, org_id=org_id, embedder=embed, source_kind=source_kind
        )
        all_ids.extend(ids)
    return {"files": len(files), "chunks": len(all_ids), "ids": all_ids}
