"""
Task 14C.7L PART G / 14C.7M STEP 1-3 — establish, read-only, how a disposable Shopify order would
actually be disposed of in Admin API 2026-07, and persist the answer as machine-readable evidence.

Nothing here may mutate. That is enforced *before* the network, not reported afterwards: every
document this script can send is registered in `DOCUMENTS`, and `send()` refuses any document that is
not a single top-level `query` unless the whole run has first passed the static gate. The client it
uses (`ShopifyReadOnlyClient.execute_graphql`) will post any document it is handed, so the guard
cannot live there; it lives here, in the only file that is allowed to answer for this run.

`--guard-selftest` proves the guard by handing it a mutation and asserting it refused, with no client,
no credential and no request. That is the point of STEP 6: a refusal path that only fires after the
bytes left the process is not a refusal path.

What introspection can settle, and what it cannot, are different claims and are kept apart on purpose:

  SETTLEABLE   an operation's name, arguments and types, its input object shape, its payload type and
               fields, whether it is deprecated, whether it names an Order or a DraftOrder, and which
               cancellation-marker fields those objects carry.
  NOT          whether the operation succeeds before or after `draftOrderComplete`, what it does to
               `displayFinancialStatus`, and which access scope it requires. Shopify's schema does not
               express preconditions, state transitions or scope grants. Those are recorded as
               UNKNOWN — NOT EXPOSED BY INTROSPECTION, and any task that needs them must establish
               them some other way, against a real disposable order, with real authority to do it.

Writes `merchant-channel/schema/cancel_2026-07.json`. The evidence carries no token, no secret, no
signature and no store credential — names, types and deprecation text only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from app.tools.shopify.sanitizer import redact

EVIDENCE_PATH = Path(
    os.environ.get("CANCEL_EVIDENCE_PATH", "/merchant/schema/cancel_2026-07.json")
)
UNKNOWN = "UNKNOWN — NOT EXPOSED BY INTROSPECTION"

# Every question introspection is structurally unable to answer. Declared as data, so a missing answer
# in the evidence is a listed unknown rather than an empty field someone might later "fill in".
OUT_OF_REACH = {
    "pre_completion_behavior": UNKNOWN,
    "post_completion_behavior": UNKNOWN,
    "display_financial_status": UNKNOWN,
    "scope_evidence": (
        f"{UNKNOWN} — Shopify does not expose required access scopes through the schema. The only "
        "scope evidence this repository has is RESOLVE's own granted set, read live from "
        "`currentAppInstallation { accessScopes { handle } }`, which says what RESOLVE may do and "
        "nothing about what a cancellation operation would need."
    ),
}


# --------------------------------------------------------------------------- documents (all queries)
MUTATION_FIELD_NAMES = """
query Preflight14c7mMutationFieldNames {
  __type(name: "Mutation") {
    fields {
      name
      isDeprecated
      deprecationReason
      args { name type { name kind ofType { name kind ofType { name kind ofType { name } } } } }
      type { name kind ofType { name kind ofType { name } } }
    }
  }
}
"""

# Type references nest: `[OrderUserError!]!` is NON_NULL(LIST(NON_NULL(Object))), four levels deep.
# A shallower selection resolves the innermost object to `null` and renders as "[?]!", which reads as
# "Shopify didn't say" when the truth is "this script stopped looking". Five levels covers it.
_TYPE_REF = (
    "name kind ofType { name kind ofType { name kind ofType { name kind ofType { name kind } } } }"
)

_TYPE_REF = (
    "name kind ofType { name kind ofType { name kind ofType { name kind ofType { name kind } } } }"
)

ONE_TYPE = """
query Preflight14c7mOneType($name: String!) {
  __type(name: $name) {
    name
    kind
    description
    inputFields { name type { __TYPE_REF__ } }
    fields { name isDeprecated deprecationReason type { __TYPE_REF__ } }
    enumValues { name isDeprecated deprecationReason }
  }
}
""".replace("__TYPE_REF__", _TYPE_REF)

DOCUMENTS = {"MUTATION_FIELD_NAMES": MUTATION_FIELD_NAMES, "ONE_TYPE": ONE_TYPE}

# Cancellation-shaped by name. The filter is deliberately broad — it is the *list* that is evidence,
# including the operations that turn out not to exist.
CANCELLATION_TOKENS = ("cancel", "void", "delete", "close", "refund", "dispose")
# The subset worth deep-introspecting: the ones that could dispose of an order or a draft order, or
# void the payment behind one. `transactionVoid` matches no object name but is the only operation the
# task's own question about "void" can refer to, so name-filtering on "order" alone would have hidden
# the answer.
ORDER_SHAPED = ("order", "draftorder", "void")

_ORDER_DISPOSAL_MARKERS = (
    "cancelledAt",
    "cancelReason",
    "closedAt",
    "displayFinancialStatus",
    "currentTotalDimensions",
    "totalOutstanding",
)


# --------------------------------------------------------------------------- the read-only gate
_TOP_LEVEL = re.compile(r"^\s*(query|mutation|subscription)\b")


def classify(document: str) -> str:
    """The operation kind a document opens with, or 'anonymous' (a fragment), or 'multiple'."""
    kinds = _TOP_LEVEL.findall(document)
    if not kinds:
        return "anonymous" if document.strip() else "empty"
    return kinds[0] if len(kinds) == 1 else "multiple"


def assert_read_only(documents: dict[str, str]) -> list[str]:
    """
    Refuse the whole run if any document is not one plain query.

    Runs before any client is built, so an unsafe edit fails without ever reaching Shopify. This is
    also what `--guard-selftest` exercises.
    """
    kinds = {}
    offenders = []
    for name, document in documents.items():
        kind = classify(document)
        kinds[name] = kind
        if kind != "query":
            offenders.append(f"{name} is {kind!r}")
    if offenders:
        raise RuntimeError(
            "REFUSAL: this script sends introspection queries only, and these documents are not "
            "that: " + ", ".join(offenders) + ". Nothing was sent."
        )
    return sorted(kinds.values())


def guard_selftest() -> int:
    """
    STEP 6: prove the refusal path is local and live, without sending anything.

    A mutation document is fed to the same gate the real run uses. If the gate does not reject it,
    this exits non-zero — the script would then be unsafe to run at all, rather than unsafe to trust.
    """
    sample = {
        "innocuous_query": MUTATION_FIELD_NAMES,
        "planted_mutation": "mutation WouldCreateAnOrder {\n  draftOrderCreate(input: {}) { id }\n}",
    }
    try:
        assert_read_only(sample)
    except RuntimeError as exc:
        message = redact(str(exc))
        print(f"GUARD SELF-TEST: PASS — refused locally: {message[:200]}")
        # The clean set must still pass, or the guard is refusing everything and is not a guard.
        assert_read_only(DOCUMENTS)
        print(f"GUARD SELF-TEST: real document set is query-only and permitted: "
              f"{json.dumps(sorted(set(classify(d) for d in DOCUMENTS.values())))}")
        return 0
    print("GUARD SELF-TEST: FAIL — a mutation document passed the read-only gate. Do not run this.")
    return 1


def _type_label(node: dict) -> str:
    """Render a GraphQL type reference as a readable signature, e.g. [OrderCancelInput!]!"""
    if not node:
        return "?"
    kind = node.get("kind")
    inner = _type_label(node.get("ofType") or {})
    if kind == "NON_NULL":
        return f"{inner}!"
    if kind == "LIST":
        return f"[{inner}]"
    if kind == "OBJECT" or kind == "INPUT_OBJECT" or kind == "ENUM" or kind == "SCALAR":
        return node.get("name") or inner or "?"
    return node.get("name") or inner or "?"


def _shape(field: dict) -> dict:
    return {
        "name": field.get("name"),
        "type": _type_label(field.get("type") or {}),
        "deprecated": bool(field.get("isDeprecated")),
        "deprecation_reason": field.get("deprecationReason"),
    }


async def main() -> int:
    # The gate runs before the client exists: no credential is read, no connection opened, until the
    # document set is known to be query-only.
    assert_read_only(DOCUMENTS)

    from app.tools.shopify import get_shopify_client

    client = get_shopify_client()
    client.validate_configuration()

    sent: list[str] = []

    async def send(document: str, variables: dict | None = None) -> dict:
        kind = classify(document)
        if kind != "query":
            raise RuntimeError(f"REFUSAL: refusing to send a {kind!r} document.")
        sent.append(kind)
        return await client.execute_graphql(document, variables or {})

    async def one_type(name: str) -> dict | None:
        return (await send(ONE_TYPE, {"name": name})).get("__type")

    mutations = (await send(MUTATION_FIELD_NAMES)).get("__type") or {}
    fields = mutations.get("fields") or []
    by_name = {field.get("name"): field for field in fields}

    candidates = sorted(
        name
        for name in by_name
        if any(token in (name or "").lower() for token in CANCELLATION_TOKENS)
    )
    order_relevant = sorted(
        name
        for name in candidates
        if any(token in (name or "").lower() for token in ORDER_SHAPED)
    )
    print(f"API VERSION: {client.api_version}")
    print(f"TOTAL MUTATION OPERATIONS IN 2026-07: {len(fields)}")
    print(f"CANCELLATION-SHAPED NAMES ({len(candidates)}): {json.dumps(candidates)}")
    print(f"ORDER/DRAFT-ORDER SHAPED SUBSET ({len(order_relevant)}): {json.dumps(order_relevant)}")

    operations: list[dict] = []
    payloads_by_operation: dict[str, dict] = {}
    for name in order_relevant:
        entry = {
            "operation": name,
            "arguments": {
                arg.get("name"): _type_label(arg.get("type") or {})
                for arg in (by_name[name].get("args") or [])
            },
            "returns": _type_label(by_name[name].get("type") or {}),
            "deprecated": bool(by_name[name].get("isDeprecated")),
            "deprecation_reason": by_name[name].get("deprecationReason"),
        }
        payload_name = entry["returns"].rstrip("!")
        payload = await one_type(payload_name)
        if payload:
            payload_fields = {
                f.get("name"): _type_label(f.get("type") or {})
                for f in (payload.get("fields") or [])
            }
            payloads_by_operation[name] = {"payload_type": payload_name, "fields": payload_fields}
            entry["payload_fields"] = payload_fields
        else:
            entry["payload_fields"] = None
        entry["addresses_types"] = sorted(_operation_types(entry))
        operations.append(entry)
        print(f"OPERATION {name}: " + json.dumps(entry, sort_keys=True)[:400])

    # Every input object the order-shaped operations actually take, read from the schema rather than
    # assumed from a name.
    input_names: set[str] = set()
    for entry in operations:
        for signature in entry["arguments"].values():
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", signature):
                if token.endswith("Input"):
                    input_names.add(token)
    input_names.update({"OrderCancelInput"})
    inputs: list[dict] = []
    for type_name in sorted(input_names):
        node = await one_type(type_name)
        if node is None:
            print(f"INPUT TYPE NOT IN 2026-07 SCHEMA: {type_name}")
            inputs.append({"name": type_name, "present": False})
            continue
        inputs.append(
            {
                "name": type_name,
                "present": True,
                "kind": node.get("kind"),
                "fields": {
                    f.get("name"): _type_label(f.get("type") or {})
                    for f in (node.get("inputFields") or [])
                },
            }
        )
        print(f"INPUT {type_name}: " + json.dumps(inputs[-1]["fields"], sort_keys=True)[:300])

    # The objects those operations name, and the fields on them that record a cancellation happened.
    object_evidence: dict[str, dict] = {}
    for type_name in ("Order", "DraftOrder"):
        node = await one_type(type_name)
        names = {f.get("name") for f in (node or {}).get("fields") or []}
        object_evidence[type_name] = {
            "present": node is not None,
            "cancellation_markers_present": {
                marker: marker in names for marker in _ORDER_DISPOSAL_MARKERS
            },
            "has_id_field": "id" in names,
            "draft_order_has_order_field": ("order" in names) if type_name == "DraftOrder" else None,
        }
        print(f"OBJECT {type_name}: " + json.dumps(object_evidence[type_name], sort_keys=True)[:400])

    # Enumerations a cancellation could reference or produce. Values are schema facts; what a
    # mutation *transitions to* is not.
    enums: dict[str, dict] = {}
    for type_name in ("OrderCancelReason", "DisplayFinancialStatus", "OrderFinancialStatus"):
        node = await one_type(type_name)
        if node is None:
            enums[type_name] = {"present": False}
            print(f"ENUM NOT IN 2026-07 SCHEMA: {type_name}")
            continue
        enums[type_name] = {
            "present": True,
            "kind": node.get("kind"),
            "values": sorted(
                v.get("name") for v in (node.get("enumValues") or []) if v.get("name")
            ),
            "deprecated_values": sorted(
                v.get("name") for v in (node.get("enumValues") or []) if v.get("isDeprecated")
            ),
        }
        print(f"ENUM {type_name}: " + json.dumps(enums[type_name]["values"]))

    deprecations = sorted(
        {
            f"{entry['operation']}: {entry['deprecation_reason']}"
            for entry in operations
            if entry["deprecated"]
        }
    ) or ["none of the order/draft-order disposal-shaped operations is deprecated"]

    evidence = {
        "api_version": client.api_version,
        "read_only": True,
        "verified_by": (
            "live introspection of the Shopify Admin GraphQL schema, query documents only; "
            "0 mutations sent — see documents_sent"
        ),
        "mutation_operation_count_total": len(fields),
        "candidate_operation_names": candidates,
        "operations": operations,
        "payloads": [
            {"operation": name, **payload} for name, payload in payloads_by_operation.items()
        ],
        "inputs": inputs,
        "objects": object_evidence,
        "enums": enums,
        "deprecations": deprecations,
        # ---- structural applicability: derivable from the schema, because the type names say so
        "order_applicability": _applicability(operations, "Order"),
        "draft_order_applicability": _applicability(operations, "DraftOrder"),
        # ---- behavioural questions introspection cannot answer; recorded, never inferred
        **OUT_OF_REACH,
        "unknowns": [
            "Whether any disposal operation succeeds on a DRAFT order that has not been completed: "
            + UNKNOWN,
            "Whether cancellation of a completed order is permitted at all, and under what store "
            "conditions: " + UNKNOWN,
            "The value `displayFinancialStatus` takes after cancellation (e.g. whether it becomes "
            "VOIDED): " + UNKNOWN,
            "The access scope each disposal operation requires: " + UNKNOWN,
            "Whether cancellation is reversible: " + UNKNOWN,
            "Whether a cancelled order remains readable by `read_orders` in the same shape: "
            + UNKNOWN,
        ],
        "documents_sent": {"count": len(sent), "kinds": sorted(set(sent))},
        "credential_fields_included": False,
    }

    if any(kind != "query" for kind in sent):
        print("REFUSAL: a non-query document was sent from a schema-verification script.")
        return 1

    print("OPERATION SHAPES: " + json.dumps(operations, indent=2, sort_keys=True))
    try:
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(
            json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"EVIDENCE WRITTEN: {EVIDENCE_PATH}")
    except OSError as exc:
        print(f"EVIDENCE NOT WRITTEN (read-only mount?): {redact(str(exc))[:120]}")
        return 2

    print(f"DOCUMENTS SENT: {json.dumps(evidence['documents_sent'])}")
    print("VERDICT: read-only introspection completed; no mutation was sent.")
    return 0


def _type_tokens(signature: str) -> set[str]:
    """The bare type names in a rendered signature, with wrappers and field labels removed."""
    return {token for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", signature or "")}


def _operation_types(entry: dict) -> set[str]:
    """
    Every named type one operation touches, on both sides.

    Arguments and payload fields together, because "this operation addresses an Order" has to be a
    statement about types. It is not a substring test on a name: `FulfillmentOrder` and `DraftOrder`
    both *contain* "Order", and treating them as Orders would have made this script report five
    fulfillment operations as order disposal for the merchant's own purchase.
    """
    tokens: set[str] = set()
    for signature in entry["arguments"].values():
        tokens |= _type_tokens(signature)
    for signature in (entry.get("payload_fields") or {}).values():
        tokens |= _type_tokens(signature)
    return tokens


def _applicability(operations: list[dict], type_name: str) -> str:
    """
    Which disposal-shaped operations the schema ties to an object, on two separate signals.

    An operation can be *addressed at* an object two ways, and conflating them would make the
    evidence lie in either direction:

      TYPES     an argument or payload field whose type is the object itself — `orderClose` hands
                back an `Order`, so its answer is that object.
      ARG NAMES an argument *named* for the object but typed as a bare `ID!` — `orderCancel` and
                `orderDelete` both take `orderId: ID!`. Shopify's schema says "an id"; only the
                name says whose. That is weaker, and it is reported as weaker.

    Neither establishes that a call succeeds, or when. It does not establish that a name is not a
    promise: an `orderId: ID!` could still be refused for an order in some state, which is a
    behavioural question introspection cannot reach.
    """
    lower = type_name.lower()
    by_type = sorted(entry["operation"] for entry in operations if type_name in _operation_types(entry))
    # A type *composed on* the object — `DraftOrderDeleteInput` for `DraftOrder` — says the same thing
    # about which object an operation takes, just not with the bare object name. Reporting only the
    # exact match would leave `draftOrderDelete` looking unrelated to a draft order, which is a
    # misleading negative rather than a finding.
    by_composed = sorted(
        entry["operation"]
        for entry in operations
        if any(
            token != type_name and token.startswith(type_name)
            for token in _operation_types(entry)
        )
    )
    by_name = sorted(
        entry["operation"]
        for entry in operations
        if any(lower in (argument or "").lower() for argument in entry["arguments"])
    )
    parts = [
        "names it as a type: " + (json.dumps(by_type) if by_type else "none"),
        "names a type composed on it (e.g. an input/payload object): "
        + (json.dumps(by_composed) if by_composed else "none"),
        "takes an argument named for it: " + (json.dumps(by_name) if by_name else "none"),
    ]
    return (
        "; ".join(parts)
        + " — structural only; whether any call is valid at a given point in the object's "
        "lifecycle is " + UNKNOWN
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--guard-selftest",
        action="store_true",
        help="prove the read-only gate refuses a mutation document locally, sending nothing",
    )
    args = parser.parse_args()
    if args.guard_selftest:
        raise SystemExit(guard_selftest())
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print("SCHEMA VERIFICATION FAILED:", redact(str(exc))[:400])
        raise SystemExit(2)
