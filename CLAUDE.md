# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp bot for Club Costa Azul (Uruguay). Receives forwarded Meta webhook payloads from an upstream service, processes them through a conversation state machine, and replies via the Graph API. It also calls a Club Costa Azul services API for member (socio) and activity data.

## Commands

```bash
# Install dependencies
uv sync

# Run locally (hot-reload)
uv run uvicorn app.main:app --reload --port 8000

# Docker
docker build -t costa-azul-bot .
docker run --env-file .env -p 8000:8000 costa-azul-bot
```

Tests: `uv run pytest tests/`. Lint/format: `uv run ruff check .` and
`uv run ruff format --check .`. Tests use synthetic settings and mocked transports;
they never contact Meta or send messages to real members.

## Architecture

```
Meta Cloud API → upstream webhook (forwards raw payload) → POST /webhook (this bot)
this bot → Graph API (send messages to users)
this bot → Club Costa Azul services API (socios / actividades / inscripciones)
```

This service does **not** handle Meta webhook verification or receive Meta's webhook directly. An upstream service forwards the raw payload unmodified.

### Key modules

- **`app/main.py`** — FastAPI app with `/webhook` (POST) and `/health` (GET). Always returns 200 to prevent upstream retries.
- **`app/conversation.py`** — Thin router: sign-in → global keyword middleware → flow dispatch → main menu. Entry point: `handle_message()`. Does not contain flow logic.
- **`app/state.py`** — In-memory `SessionStore` keyed by phone number, with per-phone `asyncio.Lock`. `Session` holds member/flow state, versioned confirmation IDs, deferred completion and task counters. Conversation state remains volatile; Mongo history/telemetry does not persist the state machine.
- **`app/flows/base.py`** — `BaseFlow[T]` ABC (generic over the flow's state dataclass) and `FlowResult` StrEnum. All flows subclass this.
- **`app/flows/activities.py`** — Activity sign-up flow. `ActivitiesStep` StrEnum, `ActivitiesState` dataclass, `ActivitiesFlow` implementation.
- **`app/flows/__init__.py`** — `FLOW_REGISTRY` mapping menu item IDs to flow instances. Adding a flow = one entry here + one `MENU_OPTIONS` row in `conversation.py`.
- **`app/services_client.py`** — Async `httpx` client wrapping the Club Costa Azul API. Endpoints include `/socios/by-whatsapp/{number}`, `/actividades`, `/socios/{id}/inscripciones`, `/socios/{id}/cuotas`, and `/inscripciones`. Protected bot calls send `X-Api-Key` when `BOT_API_KEY` is configured; errors raise `ServicesAPIError`.
- **`app/whatsapp_client.py`** — Async `httpx` client for the Meta Graph API. Supports `send_text`, `send_buttons` (max 3), and `send_list` (max 10 rows). Sending failures are logged, not raised.
- **`app/webhook_parser.py`** — Parses raw Meta payload into `IncomingMessage` dataclasses. Handles `text` and `interactive` (list_reply, button_reply) message types.
- **`app/config.py`** — `pydantic-settings` config loaded from `.env`.

### Adding new flows

See **[docs/flows.md](docs/flows.md)** for the full guide. In short: create a flow module in `app/flows/` with a `StrEnum` for steps, a state dataclass, and a `BaseFlow` subclass; register it in `FLOW_REGISTRY`; add a menu row.

### Conversation flow (Spanish)

All user-facing messages are in Spanish. The bot auto-identifies the user by WhatsApp number (no login prompt), shows their current activity enrollments, offers available activities, and confirms sign-up via interactive buttons.

KAN-271: activity signup selects a future class with capacity when available, persists
`instanciaId`, and keeps generic signup only when no future scheduled classes exist
(never as a way around full classes). Enrollment reads include `clases` and their
`estadoClase`; per-class withdrawal uses the instance endpoint with `socioId`, not
whole-inscription deletion. Confirmations reload API data; lost sessions cannot replay
old selections. See `docs/flows.md` for contracts and pagination.

### WhatsApp API constraints

KAN-242: catalogs open immediately, summaries adapt to the body budget, singleton
class/cuota choices skip redundant selectors, and completion shares the main-menu
message. Use `session.finish(body, outcome)` instead of sending terminal text.
Confirmation buttons must use `confirmation_buttons(..., session=session)` and
writes must consume the exact versioned ID with `session.consume_confirmation`.
Do not send actionable controls after an unconfirmed required overflow send.
See `docs/message-efficiency.md` for tested budgets, telemetry and Mongo queries.

- Button titles: max 20 chars
- List section titles: max 24 chars
- List row descriptions: max 72 chars
- Max 3 buttons per message, max 10 rows per list
- These limits are enforced via `_truncate()` in `whatsapp_client.py`.
- The storing wrapper splits necessary overflow losslessly. Bodies are bounded:
  text 4096, buttons 1024, lists conservatively 1024 for the current API version.
  `WHATSAPP_LIST_BODY_LIMIT` can be raised only after verifying version support.

## Environment Variables

Copy `.env.example` to `.env`. Required vars: `WHATSAPP_API_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `SERVICES_API_BASE_URL`. Optional: `BOT_API_KEY`, `WHATSAPP_API_VERSION` (default `v20.0`), `LOG_LEVEL` (default `INFO`).

## Deployment

Deployed to Railway via Dockerfile. Railway injects `$PORT` at runtime; the CMD respects it.
