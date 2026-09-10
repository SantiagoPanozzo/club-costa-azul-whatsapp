from app.flows.activities import ActivitiesFlow, ActivitiesState, ActivitiesStep
from app.state import Session, SessionStore


async def test_process_local_message_claim_is_atomic_for_repeated_wamid():
    store = SessionStore()

    assert await store.claim_message("wamid.1")
    await store.mark_message_processed("wamid.1")
    assert not await store.claim_message("wamid.1")
    assert await store.claim_message("wamid.2")


async def test_process_local_mutation_marker_survives_response_retry():
    store = SessionStore()

    assert not await store.was_mutation_applied("wamid.confirm", "create-reservation")
    await store.mark_mutation_applied("wamid.confirm", "create-reservation")
    assert await store.was_mutation_applied("wamid.confirm", "create-reservation")
    assert not await store.was_mutation_applied("wamid.other", "create-reservation")


def test_persisted_flow_state_is_rehydrated_to_its_typed_dataclass():
    session = Session(
        active_flow="activities",
        flow_state={
            "step": "awaiting_confirm",
            "available_activities": {"a1": {"id": "a1"}},
            "selected_activity": {"id": "a1"},
            "activities_by_id": {},
            "active_inscriptions": {},
            "selected_inscription": None,
        },
    )

    state = ActivitiesFlow().get_state(session)

    assert isinstance(state, ActivitiesState)
    assert state.step == ActivitiesStep.AWAITING_CONFIRM
    assert state.selected_activity == {"id": "a1"}
