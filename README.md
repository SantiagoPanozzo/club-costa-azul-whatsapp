# Club Costa Azul WhatsApp bot

Stateful Spanish-language WhatsApp bot for club members. It receives signed payloads from the separate webhook router, identifies an active member by WhatsApp number, calls the club API with a server-only bot key, and sends replies through Meta's Graph API.

## Supported member journeys

- Activities: list active enrollments and available activities, enroll, and cancel.
- Quotas: list dues and link to the configured payment page.
- Reservations: list/cancel own reservations and submit a new request with date, time, party size, reason, and notes. New requests remain `Pendiente` until staff approval.
- Events: list, enroll, withdraw, and re-enroll after a cancellation.
- Personal data: show the member's own record and link to the configured profile page.
- News: show currently published news.
- Navigation: interactive menu plus Spanish text/number aliases, `menu`, `volver`, `ayuda`, `contacto`, and human-contact guidance.

Long WhatsApp lists are split into pages of at most ten rows. Unsupported input returns the user to a usable menu.

## Delivery and state model

```text
Meta Cloud API -> webhook router -> signed POST /webhook -> bot
bot -> Club API (X-Api-Key)
bot -> Meta Graph API
bot <-> Redis (sessions, per-phone locks, message and mutation deduplication)
bot -> MongoDB (optional conversation trace)
```

The router signs the exact body with `ROUTER_SHARED_SECRET`, a Unix timestamp, and HMAC-SHA256. The bot rejects missing, invalid, or stale signatures. Redis stores sessions with a TTL, serializes each phone's conversation across replicas, records completed inbound messages, and records successful API mutations before sending their confirmation. This lets router retries recover from an outbound Meta failure without repeating the mutation.

MongoDB is optional telemetry. Failure to initialize or write traces does not stop conversation handling. Set `REQUIRE_REDIS=true` in staging and production; `/health` then returns 503 unless shared Redis is available.

## Configuration

Copy `.env.example` to `.env`. Required application variables are:

| Variable | Purpose |
| --- | --- |
| `WHATSAPP_API_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | Meta outbound delivery |
| `SERVICES_API_BASE_URL`, `BOT_API_KEY` | Club API and server-only bot authentication |
| `ROUTER_SHARED_SECRET` | Router-to-bot request authentication |
| `FRONTEND_URL`, `CLUB_CONTACT_TEXT` | User-facing links and support guidance |
| `SESSION_REDIS_URL` | Shared sessions, locks, and deduplication in deployed environments |

Optional settings include `WHATSAPP_API_VERSION`, `SESSION_TTL_SECONDS`, `MEMBER_REVALIDATE_SECONDS`, `MONGODB_URL`, `REQUIRE_REDIS`, and `LOG_LEVEL`.

## Development and verification

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run uvicorn app.main:app --reload --port 8000
```

The real Redis integration test is opt-in:

```bash
REDIS_TEST_URL=redis://127.0.0.1:6379/15 uv run pytest tests/test_redis_state_integration.py -q
```

Build the release image with `docker build -t costa-azul-bot .`. The Dockerfile uses Python 3.12 and installs the exact checked-in `uv.lock` without resolving dependencies during startup.

The complete flow design is in [docs/flows.md](docs/flows.md). Coordinated release status and integration evidence are in the root [BOT_STABLE_RELEASE_STATUS.md](../BOT_STABLE_RELEASE_STATUS.md).
