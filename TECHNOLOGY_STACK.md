# TECHNOLOGY STACK

All versions are the pinned values in the repository manifests (`backend/requirements.txt`,
`frontend/package.json`). Runtime deployment is Docker Compose on a single host.

## Backend

| Component | Version | Role |
| --- | --- | --- |
| Python | 3.x (Docker image) | runtime |
| FastAPI | 0.115.0 | HTTP framework |
| Starlette (via FastAPI) | 0.38.x | ASGI (multipart, middleware) |
| pydantic / pydantic-settings | 2.9.2 / 2.5.2 | validation + config |
| SQLAlchemy | 2.0.35 | ORM (async) |
| asyncpg | 0.29.0 | PostgreSQL driver |
| Alembic | 1.13.2 | migrations |
| razorpay | 1.4.2 | Razorpay SDK (TEST mode only) |
| httpx | 0.27.2 | outbound HTTP (providers, SigV4 store) |
| websockets | 13.1 | WS support |
| bcrypt | 4.2.0 | password hashing |
| python-multipart | 0.0.33 | multipart form parsing |
| pytest / pytest-asyncio | 8.3.3 / 0.24.0 | test runner |
| aiosqlite | 0.20.0 | hermetic test DB option |
| aiohttp | 3.9.5 | test/runtime HTTP |

## Frontend

| Component | Version | Role |
| --- | --- | --- |
| Next.js | 14.2.13 | App Router framework |
| React / React DOM | 18.3.1 | UI |
| TypeScript | 5.6.2 | types (`tsc --noEmit` gate) |
| Tailwind CSS | 3.4.12 | styling (token system) |
| vitest | ^2.1.9 | frontend tests (35 tests, repo-first) |
| lucide-react | (per package.json) | icons |

## Infrastructure

| Component | Version/notes | Role |
| --- | --- | --- |
| PostgreSQL | 16 | primary store (async, role-separated) |
| MinIO | local S3-compatible | evidence bucket; ledger conformance target |
| Docker Compose | single host | backend + frontend + db + minio |
| Node.js | 24.x (build host) | frontend build/test |

## External services (all TEST / free tier)

| Service | Mode | Verified behavior |
| --- | --- | --- |
| Razorpay | **TEST mode only** | order create, checkout, HMAC-SHA256 verify, refund executed in TEST mode |
| SerpAPI | free tier | Google Shopping discovery (real results live) |
| Google Gemini | API | primary reasoning provider (live two-turn verified previously) |
| Groq | API | failover reasoning provider (live; cooldown circuit breaker) |
| Shopify Admin API | read-only + separate channel | catalog read verified live; merchant purchase only via the separate trigger process |

## Security-relevant stack facts

- Passwords: bcrypt; sessions: HTTP-only cookie.
- Money: integers in paise end-to-end server-side (`amount_paise`), Decimal for money math;
  the frontend never converts currency.
- Evidence: sha256 pinning; structural image validation; streaming size caps; no client-side
  assertions of storage state.
- Secrets: never printed/committed; redaction module in logging; no storage keys, bucket
  names or credentials in API responses (verified live).
