# Conversation Flows

## Architecture

The bot uses a flow-based architecture for multi-step conversations. Each flow is a self-contained module that owns its steps, state, and message handling.

```
User message
  → shared per-phone Redis lock + completed-message check
  → webhook_parser (raw payload → IncomingMessage)
  → conversation.handle_message (router)
      → sign-in check (auto-identify by phone number)
      → global keyword middleware ("menu", "volver", etc.)
      → active flow dispatch (or main menu if no flow is active)
```

### Key components

- **`conversation.py`** — Thin router. Handles sign-in, global keywords, main menu, and dispatches to the active flow. Does not contain flow logic.
- **`flows/base.py`** — `BaseFlow[T]` ABC and `FlowResult` enum.
- **`flows/*.py`** — One module per flow (e.g., `activities.py`).
- **`state.py`** — Redis-backed `SessionStore`; `Session` holds `socio`, `active_flow`, typed `flow_state`, and member revalidation time. Local memory is only a development fallback.

### Message handling chain

1. **Lock/deduplication**: Serialize the phone's messages, skip a WhatsApp message ID only after its earlier delivery completed successfully, and reload its session.
2. **Sign-in/revalidation**: Look up a new member by phone and periodically revalidate a cached member.
3. **Global keywords**: If the user texts `menu`, `volver`, `ayuda`, etc., end the current flow and show the relevant response/menu.
4. **Active flow**: If `session.active_flow` is set, dispatch to that flow's `handle()` method.
5. **Menu selection**: Accept the interactive ID or its documented text/number alias.
6. **Fallback**: Show the main menu.

## How to add a new flow

### 1. Define the step enum and state dataclass

```python
# app/flows/my_flow.py
from dataclasses import dataclass
from enum import StrEnum


class MyFlowStep(StrEnum):
    FIRST_STEP = "first_step"
    AWAITING_CONFIRM = "awaiting_confirm"


@dataclass
class MyFlowState:
    step: MyFlowStep = MyFlowStep.FIRST_STEP
    # Add flow-specific fields here
    some_data: str | None = None
```

Every flow state must have a `step` field typed as a `StrEnum` subclass. This is the flow's internal state machine — use it to know which handler to call.

### 2. Implement the flow class

```python
from .base import BaseFlow, FlowResult
from ..state import Session
from ..webhook_parser import IncomingMessage


class MyFlow(BaseFlow[MyFlowState]):
    def create_state(self) -> MyFlowState:
        return MyFlowState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        # Send the first message to the user
        await whatsapp_client.send_text(phone, "Welcome to my flow!")

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == MyFlowStep.FIRST_STEP:
            return await self._handle_first(phone, session, state, msg)
        if state.step == MyFlowStep.AWAITING_CONFIRM:
            return await self._handle_confirm(phone, session, state, msg)

        return FlowResult.DONE
```

Key rules:
- `enter()` must call `self.create_state()` and assign it to `session.flow_state`.
- `handle()` returns `FlowResult.CONTINUE` to stay in the flow, `FlowResult.DONE` to return to the main menu.
- Use `self.get_state(session)` to retrieve the typed state inside `handle()`.
- When `handle()` returns `DONE`, the router automatically sends the main menu — don't send it yourself.

### 3. Register the flow

```python
# app/flows/__init__.py
from .my_flow import MyFlow

FLOW_REGISTRY: dict[str, BaseFlow] = {
    "activities": ActivitiesFlow(),
    "my_flow": MyFlow(),  # <-- add here
}
```

### 4. Add a menu entry

```python
# app/conversation.py
MENU_OPTIONS: list[dict[str, str]] = [
    {
        "id": "activities",  # must match FLOW_REGISTRY key
        "title": "Actividades",
        "description": "Ver tus actividades e inscribirte",
    },
    {
        "id": "my_flow",  # must match FLOW_REGISTRY key
        "title": "Mi Flujo",
        "description": "Descripción corta del flujo",
    },
]
```

The menu item `id` must match the `FLOW_REGISTRY` key exactly — this is how the router knows which flow to enter.

## Conventions

- All user-facing text is in Spanish.
- Flow-specific constants (button IDs, prefixes) live in the flow module, not in shared constants.
- Errors from the services API should be caught, logged, and result in a generic error message to the user. Don't leak internal details.
- WhatsApp limits: button titles max 20 chars, list row titles max 24 chars, list row descriptions max 72 chars, max 3 buttons, max 10 list rows. These are enforced by `whatsapp_client.py` via truncation, but keep your text concise.
- Use `session.end_flow()` if a flow needs to exit early (e.g., no data available). The router will handle showing the menu.
- Wrap every externally visible mutation with `sessions.was_mutation_applied()` / `mark_mutation_applied()` using the inbound `wamid`. Mark it after the API succeeds and before sending confirmation.
- Use `send_list_pages()` for dynamic row sets so items beyond WhatsApp's ten-row limit are not hidden.
