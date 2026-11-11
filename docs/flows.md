# Conversation Flows

## Architecture

The bot uses a flow-based architecture for multi-step conversations. Each flow is a self-contained module that owns its steps, state, and message handling.

```
User message
  → webhook_parser (raw payload → IncomingMessage)
  → conversation.handle_message (router)
      → sign-in check (auto-identify by phone number)
      → global keyword middleware ("menu", "ayuda", etc.)
      → active flow dispatch (or main menu if no flow is active)
```

### Key components

- **`conversation.py`** — Thin router. Handles sign-in, global keywords, main menu, and dispatches to the active flow. Does not contain flow logic.
- **`flows/base.py`** — `BaseFlow[T]` ABC and `FlowResult` enum.
- **`flows/*.py`** — One module per flow (e.g., `activities.py`).
- **`state.py`** — `Session` holds `socio`, `active_flow` (registry key), and `flow_state` (typed per-flow dataclass).

### Message handling chain

1. **Sign-in**: If `session.socio` is `None`, look up the user by phone number. No further processing.
2. **Global keywords**: "menu", "menú" and "inicio", or the explicit `global_menu` button, exit to the main menu. "ayuda"/"help" shows help. Volver buttons belong to each flow and return to its previous selector, not the main menu.
3. **Active flow**: If `session.active_flow` is set, dispatch to that flow's `handle()` method.
4. **Menu selection**: If the user picked a menu item whose `id` matches a flow registry key, enter that flow.
5. **Fallback**: Show the main menu.

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
- To finish, call `session.finish("Resultado en español", outcome="success")` and return `DONE`. The router puts the result and menu in one message. An immediate `enter()` result also uses `session.finish()` and returns `None`.
- Use explicit outcomes such as `error`, `empty`, `unavailable` or `conflict`; a bare `DONE` has an `unknown` outcome, not a fabricated success.
- Send through `storing_client`, always passing `session=session`, so every wire attempt belongs to its task run.
- Build confirmation buttons with `confirmation_buttons(YES, BACK, session=session)` from `flow_ui`. Accept writes only after `session.consume_confirmation(msg.interactive_id, YES)` succeeds. Nonces bind consent to the current selection/terms; do not accept the unversioned action ID.
- Catalog/list rendering clears old confirmations; changed terms consume the old version and issue a fresh one. A plain invalid-input retry may reuse the unchanged current version.

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

## Actividades: clases puntuales (KAN-271)

- Actividades abre directamente el catálogo con una opción **Mis inscripciones**. Una actividad
  no se excluye por tener otra inscripción ni por su cupo genérico: al elegirla se
  consulta `GET /actividades/{id}/instancias`, con `cupoDisponible` por clase.
- Se ofrecen clases futuras `Programada`, no liquidadas, con cupo y aún no cubiertas
  por una inscripción activa. Fechas y horarios usan `America/Montevideo` y días en
  español. Una única clase elegible pasa directamente a confirmación, sin alta automática.
  La selección pagina reservando filas para navegación y acceso al catálogo (máximo diez en total).
- El POST envía `{ socioId, actividadId, instanciaId }`. Si no hay clases futuras
  programadas, conserva `{ socioId, actividadId }` y confirma explícitamente una
  inscripción general. **Las clases llenas/cerradas/ya cubiertas no habilitan ese
  fallback**, para no eludir el cupo mediante una inscripción genérica.
- Mis inscripciones muestra cada clase con actividad, fecha, horario y bajas. Para
  genéricas se muestra también una opción separada para cancelar el rango completo.
  Las clases cubiertas por una genérica aparecen individualmente y se deduplican si
  se superponen con una inscripción específica. Las inscripciones históricas,
  clases pasadas y bajas registradas siguen accesibles como detalle de solo lectura;
  nunca desplazan una opción activa ni habilitan cancelar de nuevo.
- Una baja de clase llama `DELETE /instancias/{id}/inscripcion?socioId={socioId}` con
  `X-Api-Key`; nunca usa `DELETE /inscripciones/{id}`. Esa última operación se conserva
  solo para cancelar explícitamente una inscripción **genérica completa**.
- Antes del alta se recarga la clase y, si cambió fecha/horario/costo, se vuelve a pedir
  confirmación. Antes de la baja se lee `GET /inscripciones/{id}` y se valida dueño,
  estado y clase. La API vuelve a verificar disponibilidad al ejecutar. Perder la
  sesión obliga a recomenzar: ningún botón viejo ejecuta una operación sin selección.
- Cupo agotado, recurso retirado/ya cancelado y fallos técnicos tienen mensajes
  acotados en español; nunca se reenvían cuerpos o excepciones internas al usuario.
- Tests: `uv run pytest tests/test_activities.py tests/test_services_client.py`.
  No requieren Meta ni números reales; las salidas WhatsApp se simulan.

## Convenciones generales

- All user-facing text is in Spanish.
- Flow-specific constants (button IDs, prefixes) live in the flow module, not in shared constants.
- Errors from the services API should be caught, logged, and result in a generic error message to the user. Don't leak internal details.
- WhatsApp limits: button titles max 20 chars, list row titles max 24 chars, list row descriptions max 72 chars, max 3 buttons, max 10 list rows. These are enforced by `whatsapp_client.py` via truncation, but keep your text concise.
- Use `session.finish(body, outcome)` for an early terminal result. `end_flow()` only clears volatile state; it does not declare a successful task.
- Shared bounded summaries, navigation, message budgets and telemetry are documented in [message-efficiency.md](message-efficiency.md).
