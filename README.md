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

## Repository layout

```
resolve/
├── backend/            FastAPI service — Mission engine, Authority Engine, negotiation
│                        protocol, Razorpay integration, WebSocket activity stream
├── frontend/           Next.js app — marketing landing page + the agent workspace
├── docs/               Product specification
└── docker-compose.yml  Runs backend + Postgres + frontend together
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
