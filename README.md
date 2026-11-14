# RESOLVE

**The settlement layer for agent-to-agent commerce disputes.**

RESOLVE is an autonomous agent that takes over the instant a payment completes. It holds the
buyer's real objective (not just "buy item X"), watches the real transaction against real
merchant, logistics, and payment systems, detects when reality deviates from what was promised,
and negotiates a bounded recovery — directly with a Merchant Agent when one exists, or with the
merchant themselves — until the objective is verified as restored.

Full product spec: [`docs/RESOLVE_Product_Spec.md`](docs/RESOLVE_Product_Spec.md)
The buyer-side agent loop — its declared tools, its context, and its boundaries:
[`docs/continuous-agent-loop.md`](docs/continuous-agent-loop.md)

## Submission documentation

| Document | Contents |
| --- | --- |
| [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) | What the product does — verified vs simulated, plainly labelled |
| [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) | Components, data flow, trust boundaries |
| [MODULE_DESCRIPTION.md](MODULE_DESCRIPTION.md) | Backend + frontend module-by-module |
| [TECHNOLOGY_STACK.md](TECHNOLOGY_STACK.md) | Pinned dependency versions |
| [API_DOCUMENTATION.md](API_DOCUMENTATION.md) | HTTP surface (43 endpoints + WebSocket) |
| [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | The 14 live tables |
| [SECURITY_AND_AUTHORITY.md](SECURITY_AND_AUTHORITY.md) | Auth, ownership, authority model, evidence security, audit |
| [TESTING_AND_VALIDATION.md](TESTING_AND_VALIDATION.md) | Suite results, live verification, known failures |
| [LIMITATIONS_AND_FUTURE_SCOPE.md](LIMITATIONS_AND_FUTURE_SCOPE.md) | What does not work / is not claimed |
| [DEMO_GUIDE.md](DEMO_GUIDE.md) | Step-by-step demo script |

## Status (2026-11-14)

- **Runs locally via Docker Compose. Not deployed to production.**
- Verified live in a real browser: discovery → selection → Razorpay TEST payment (HMAC-verified
  server-side) → order record (demo-simulated delivery, honestly labelled) → issue intake →
  evidence upload (MinIO, sha256-pinned, authenticated retrieval) → evidence-aware reassessment
  (eligibility upgrades, authority still ESCALATE, nothing executed) → authority traces and
  audit trail.
- Razorpay is **TEST mode only**. A TEST-mode refund (`rfnd_Tyvnf5oHEKqJXU`) was executed and
  provider-verified. No real money moves anywhere in the system.
- The canonical test suite matches its frozen baseline name-for-name (13 failed / 1568 passed /
  67 skipped / 12 errors — all pre-existing run-environment artifacts, documented in
  TESTING_AND_VALIDATION.md). It is not a green run and is not presented as one.

## Repository layout

```
resolve/
├── backend/            FastAPI service — Mission engine, Authority Engine, negotiation
│                        protocol, Razorpay integration (TEST mode), evidence storage
│                        (MinIO), audit log, optional event ledger, WebSocket stream
├── frontend/           Next.js app — marketing landing page + the agent workspace
├── merchant-channel/   Separate trigger process for merchant purchases (never imported
│                        in-process); not configured in the demo deployment
├── docs/               Product specification + topic docs
└── docker-compose.yml  Runs backend + Postgres + frontend + MinIO together
```

## Running it

### With Docker (recommended)

```bash
cp backend/.env.example backend/.env
# fill in RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (test mode) and an LLM API key
docker compose up --build
```

- Backend API + WebSocket: http://localhost:8000
- Frontend (landing page + workspace): http://localhost:3000

### Backend only, without Docker

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

### Frontend only, without Docker

```bash
cd frontend
npm install
npm run dev
```

## The core idea, in three files

If you only read three files in this repo, read these — they are the whole safety argument:

1. **`backend/app/authority/engine.py`** — the deterministic Authority Engine. Every money-moving
   or irreversible action, on either the Buyer Agent's or the Merchant Agent's side, passes
   through this pure function before it is allowed to execute. It has no model calls and no
   network calls. It is the only component in the system allowed to approve spend.
2. **`backend/app/protocol/negotiation.py`** — the claim → evidence → offer → counter → accept
   state machine two agents use to settle a dispute without a human in the loop.
3. **`backend/app/models/mission.py`** — the Mission and Recovery Mandate schemas: the structured
   object that survives from purchase through to verified resolution, replacing a chat transcript
   as the system's source of truth.

## Testing the Authority Engine in isolation

```bash
cd backend
pytest tests/test_authority_engine.py -v
pytest tests/test_negotiation_protocol.py -v
```

These tests do not require Postgres, Razorpay, or an LLM key — the Authority Engine and the
negotiation state machine are both pure, dependency-free Python, by design.
