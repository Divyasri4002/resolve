"""
The durable purchase-event ledger and external evidence publisher (Phase 4.6.2).

Modules:
  canonical — resolve-canonical-json/1: envelope, head, chain hash (pure, no I/O).
  store     — minimal S3-compatible client; conditional PUTs only (If-Match ETag,
              If-None-Match:* for genesis). No unconditional overwrite exists.
  publisher — the lease-serialized publication state machine.
  verify    — the independent read-only verifier, the submission currency gate,
              and the `python -m app.ledger.verify` CLI.
"""
from app.ledger import canonical

__all__ = ["canonical"]
