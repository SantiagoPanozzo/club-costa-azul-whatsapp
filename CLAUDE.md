# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp bot for Club Costa Azul (Uruguay). Receives HMAC-authenticated Meta payloads from the upstream router, processes member journeys through conversation state machines, calls the Club API with a bot key, and replies through Meta's Graph API. Redis provides durable sessions, distributed phone locks, message deduplication, and successful-mutation markers.

## Commands

```bash
# Install dependencies
uv sync

# Verification
uv run ruff check .
uv run ruff format --check .
uv run pytest -q

# Run locally (hot-reload)
uv run uvicorn app.main:app --reload --port 8000

# Docker
docker build -t costa-azul-bot .
docker run --env-file .env -p 8000:8000 costa-azul-bot
```

## Architecture

```
Meta Cloud API → upstream webhook (forwards raw payload) → POST /webhook (this bot)
this bot → Graph API (send messages to users)
this bot → Club Costa Azul services API (socios / actividades / inscripciones)
```

This service does **not** handle Meta webhook verification or receive Meta's webhook directly. An upstream service forwards the raw payload unmodified.

### Key modules

- **`app/main.py`** — FastAPI app with `/webhook` (POST) and `/health` (GET). Verifies the router signature; unhandled processing failures return 503 so the router queue retries.
- **`app/conversation.py`** — Thin router: sign-in → global keyword middleware → flow dispatch → main menu. Entry point: `handle_message()`. Does not contain flow logic.
- **`app/state.py`** — Async Redis-backed `SessionStore` with a local-development fallback. It persists typed flow state with TTL, locks per phone across replicas, deduplicates completed messages, and prevents a retried confirmation from repeating an API mutation.
- **`app/flows/base.py`** — `BaseFlow[T]` ABC (generic over the flow's state dataclass) and `FlowResult` StrEnum. All flows subclass this.
- **`app/flows/activities.py`** — Activity sign-up flow. `ActivitiesStep` StrEnum, `ActivitiesState` dataclass, `ActivitiesFlow` implementation.
- **`app/flows/__init__.py`** — `FLOW_REGISTRY` mapping menu item IDs to flow instances. Adding a flow = one entry here + one `MENU_OPTIONS` row in `conversation.py`.
- **`app/services_client.py`** — Async `httpx` client for member identity/details, activities, dues, spaces/reservations, events, and published news. Protected calls send `X-Api-Key`; errors raise `ServicesAPIError` or a user-facing `ServicesAPIConflict`.
- **`app/whatsapp_client.py`** — Async `httpx` client for the Meta Graph API. Supports text, buttons, and lists. Delivery failures raise `WhatsAppDeliveryError`, preventing state from advancing silently.
- **`app/webhook_parser.py`** — Parses raw Meta payload into `IncomingMessage` dataclasses. Handles `text` and `interactive` (list_reply, button_reply) message types.
- **`app/config.py`** — `pydantic-settings` config loaded from `.env`.

### Adding new flows

See **[docs/flows.md](docs/flows.md)** for the full guide. In short: create a flow module in `app/flows/` with a `StrEnum` for steps, a state dataclass, and a `BaseFlow` subclass; register it in `FLOW_REGISTRY`; add a menu row.

### Conversation flow (Spanish)

All user-facing messages are in Spanish. The bot auto-identifies the user by WhatsApp number and supports activities, dues, reservations, events, personal details, published news, and human-contact help. Interactive menus have text/number fallbacks and long lists are sent in pages.

### WhatsApp API constraints

- Button titles: max 20 chars
- List section titles: max 24 chars
- List row descriptions: max 72 chars
- Max 3 buttons per message, max 10 rows per list
- These limits are enforced via `_truncate()` in `whatsapp_client.py`.

## Environment Variables

Copy `.env.example` to `.env`. Required settings include Meta credentials, `SERVICES_API_BASE_URL`, `BOT_API_KEY`, `ROUTER_SHARED_SECRET`, and `FRONTEND_URL`. Deployments also require `SESSION_REDIS_URL` with `REQUIRE_REDIS=true`. `MONGODB_URL` is optional trace storage. See README and `.env.example` for TTL, contact, version, and logging settings.

## Deployment

Deployed to Railway via Dockerfile. Railway injects `$PORT` at runtime; the CMD respects it.
