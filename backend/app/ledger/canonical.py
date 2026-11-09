"""
resolve-canonical-json/1 — the canonical evidence representation (Phase 4.6.2).

The approved 4.5.8 D6 spec fixed the envelope's field order, required the payload to be
the exact PostgreSQL-rendered text (never re-serialized by any verifier), and required a
deterministic chain. This module is that spec in code, plus the details the approved
record left open, fixed here once and documented (not silently):

  - CHAIN HASH: H_n = sha256_hex(H_{n-1} + ":" + payload_sha256_n), H_0 = GENESIS_HASH
    (64 zeros). The verifier recomputes the chain from the EMBEDDED payload digests —
    it never re-serializes anything.
  - ENVELOPE FIELD ORDER: the D6 fields (format, event_seq, prev_seq, prev_hash,
    payload_sha256, fence_token, wall_clock, signature) keep their relative order; the
    payload-embedding fields (payload_source, payload_source_pk, payload_source_revision,
    payload_text) sit between prev_hash and payload_sha256 because the verifier must
    hash the embedded text. The dict's insertion order below IS the wire order, and
    serialize() never sorts keys.
  - fence_token: the publisher's lease epoch from ledger_publish_state — monotonic,
    and the chain requires it to be non-decreasing.
  - signature: reserved, always null. No signing infrastructure is approved; the field
    exists so the envelope does not change shape when signing is added.

Serialization is byte-stable: fixed separators, ensure_ascii=False, insertion order.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

ENVELOPE_FORMAT = "resolve-canonical-json/1"
HEAD_FORMAT = "resolve-ledger-head/1"
GENESIS_HASH = "0" * 64
GENESIS_SEQ = 0


def chain_hash(prev_head_hash: str, payload_sha256: str) -> str:
    material = f"{prev_head_hash}:{payload_sha256}".encode("ascii")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class EventRecord:
    """One purchase_events row, with the payload as the exact DB-rendered text."""

    event_seq: int
    source: str
    source_pk: str
    source_revision: int
    payload_text: str
    payload_sha256: str
    created_at: datetime


def build_envelope(event: EventRecord, *, prev_seq: int, prev_hash: str, fence_token: int) -> dict:
    return {
        "format": ENVELOPE_FORMAT,
        "event_seq": event.event_seq,
        "prev_seq": prev_seq,
        "prev_hash": prev_hash,
        "payload_source": event.source,
        "payload_source_pk": event.source_pk,
        "payload_source_revision": event.source_revision,
        "payload_text": event.payload_text,
        "payload_sha256": event.payload_sha256,
        "fence_token": fence_token,
        "wall_clock": event.created_at.isoformat() if event.created_at else None,
        "signature": None,
    }


def build_head(prev_head_hash: str, envelope: dict) -> tuple[dict, str]:
    """The head object body for one publication: the new summary plus THE event."""
    head_hash = chain_hash(prev_head_hash, envelope["payload_sha256"])
    head = {
        "format": HEAD_FORMAT,
        "head_seq": envelope["event_seq"],
        "head_hash": head_hash,
        "event": envelope,
    }
    return head, head_hash


def serialize(body: dict) -> str:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def digest_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
