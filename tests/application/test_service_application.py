from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.application.errors import (
    Conflict,
    InternalFailure,
    InvalidCommand,
    InvalidState,
    NotFound,
)
from aipcs_mcp.application.models import ApplicationContext, DesignCommand, SeedCommand
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.manifest_v2 import ManifestV2
from application.fakes import (
    BACKEND_SENTINEL,
    FakeRegistry,
    FixedClock,
    SequentialIds,
)

PRINCIPAL = "principal-secret"
CONTEXT = ApplicationContext(PRINCIPAL, "contract-test")
START = datetime(2026, 1, 1, tzinfo=UTC)
HOSTILE_SENTINELS = (
    BACKEND_SENTINEL,
    "file:" + "//host/share/database",
    "sqlite://host/database",
    "SELECT secret_value FROM records",
    "credential-token-value",
)


def build_app(
    *,
    state: FakeRegistry | None = None,
    clock: FixedClock | None = None,
    ids: SequentialIds | None = None,
) -> tuple[ServiceApplication, FakeRegistry, FixedClock, SequentialIds]:
    registry = state or FakeRegistry()
    fixed_clock = clock or FixedClock(START)
    sequential_ids = ids or SequentialIds()
    return (
        ServiceApplication(registry.uow, fixed_clock, sequential_ids),
        registry,
        fixed_clock,
        sequential_ids,
    )


def seed(
    application: ServiceApplication,
    *,
    context: ApplicationContext = CONTEXT,
    key: str = "seed-1",
    domain: str = "notes",
):
    return application.seed(
        context,
        SeedCommand(domain, "project", f"Intent for {domain}", key),
    )


def manifest() -> ManifestV2:
    return ManifestV2.model_validate(valid_manifest())


def assert_bounded(error: Exception, sentinel: str = BACKEND_SENTINEL) -> None:
    assert str(error) == InternalFailure.message
    exposed = (str(error), repr(error), repr(error.args))
    assert all(sentinel not in value for value in exposed)
    assert error.args == (InternalFailure.message,)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_seed_replay_duplicate_and_principal_scoping_are_distinct() -> None:
    application, state, clock, ids = build_app()
    original = seed(application)
    original_dump = original.model_dump(mode="json", by_alias=True)

    replay = seed(application)
    assert replay.model_dump(mode="json", by_alias=True) == original_dump
    assert (state.commits, len(state.items), len(state.events), clock.calls, ids.calls) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert state.uows[-1].calls == ["claim", "close"]

    duplicate = seed(application, key="seed-2")
    assert duplicate.model_dump(mode="json", by_alias=True) == original_dump
    assert (state.commits, len(state.items), len(state.events), clock.calls, ids.calls) == (
        2,
        1,
        2,
        2,
        1,
    )
    assert state.events[-1].outcome == "duplicate"

    other_context = ApplicationContext("other-principal", "contract-test")
    other = seed(application, context=other_context)
    assert other.service_id != original.service_id
    assert [item.service_id for item in application.list(other_context)] == [other.service_id]
    assert [item.service_id for item in application.list(CONTEXT)] == [original.service_id]
    with pytest.raises(NotFound):
        application.inspect(other_context, original.service_id)


def test_idempotency_fingerprint_includes_created_via_and_payload() -> None:
    application, state, clock, ids = build_app()
    seed(application)
    before = (state.commits, len(state.events), clock.calls, ids.calls)

    with pytest.raises(Conflict) as changed_context:
        seed(application, context=ApplicationContext(PRINCIPAL, "different-client"))
    assert str(changed_context.value) == Conflict.message

    with pytest.raises(Conflict):
        application.seed(
            CONTEXT,
            SeedCommand("other_notes", "project", "Different payload", "seed-1"),
        )
    assert (state.commits, len(state.events), clock.calls, ids.calls) == before
    assert state.uows[-1].calls == ["claim", "rollback", "close"]


def test_design_copies_input_projection_storage_and_replay() -> None:
    clock = FixedClock(START, START + timedelta(seconds=1))
    application, state, clock, ids = build_app(clock=clock)
    service = seed(application)
    requested = manifest()
    designed = application.design(
        CONTEXT,
        DesignCommand(service.service_id, requested, "design-1"),
    )
    expected = designed.model_dump(mode="json", by_alias=True)
    assert "schema" in expected and "schema_" not in expected
    assert designed.design_state == "seeded"
    assert designed.schema_version == 1

    requested.entities[0].description = "changed after write"
    designed.schema_.entities[0].description = "changed result"
    inspected = application.inspect(CONTEXT, service.service_id)
    assert inspected.model_dump(mode="json", by_alias=True) == expected

    before = (state.commits, len(state.events), clock.calls, ids.calls)
    replay = application.design(
        CONTEXT,
        DesignCommand(service.service_id, manifest(), "design-1"),
    )
    assert replay.model_dump(mode="json", by_alias=True) == expected
    assert (state.commits, len(state.events), clock.calls, ids.calls) == before
    assert state.uows[-1].calls == ["claim", "close"]

    replay.schema_.entities[0].description = "changed replay"
    assert application.inspect(CONTEXT, service.service_id).schema_.entities[0].description is None


def test_design_guards_lifecycle_scope_and_fingerprint() -> None:
    application, state, clock, ids = build_app(clock=FixedClock(START, START))
    service = seed(application)
    application.design(CONTEXT, DesignCommand(service.service_id, manifest(), "design-1"))
    before = (state.commits, len(state.events), len(state.ledger), clock.calls, ids.calls)

    with pytest.raises(InvalidState):
        application.design(CONTEXT, DesignCommand(service.service_id, manifest(), "design-2"))
    with pytest.raises(Conflict):
        application.design(
            ApplicationContext(PRINCIPAL, "different-client"),
            DesignCommand(service.service_id, manifest(), "design-1"),
        )
    with pytest.raises(NotFound):
        application.design(
            ApplicationContext("other-principal", "contract-test"),
            DesignCommand(service.service_id, manifest(), "design-other"),
        )
    assert (state.commits, len(state.events), len(state.ledger), clock.calls, ids.calls) == before


@pytest.mark.parametrize("hostile_value", ["x" * 1_001, object()])
def test_design_revalidates_mutated_manifest_before_opening_uow(hostile_value: object) -> None:
    application, state, _, _ = build_app()
    requested = manifest()
    if isinstance(hostile_value, str):
        requested.entities[0].description = hostile_value
    else:
        requested.query_patterns.append(hostile_value)

    with pytest.raises(InvalidCommand) as captured:
        application.design(CONTEXT, DesignCommand(UUID(int=1), requested, "design-1"))
    assert str(captured.value) == InvalidCommand.message
    assert state.uows == []


@pytest.mark.parametrize("failure", ["claim", "find_domain", "add", "append", "complete"])
def test_seed_failure_rolls_back_all_state_and_is_bounded(failure: str) -> None:
    application, state, _, _ = build_app()
    state.fail_at = failure

    with pytest.raises(InternalFailure) as captured:
        seed(application)
    assert_bounded(captured.value)
    assert not state.items and not state.ledger and not state.events and state.commits == 0
    assert state.uows[-1].calls[-2:] == ["rollback", "close"]
    assert state.uows[-1].close_count == 1


@pytest.mark.parametrize("failure", ["claim", "get", "save", "append", "complete", "commit"])
def test_design_failure_rolls_back_all_state_and_is_bounded(failure: str) -> None:
    application, state, _, _ = build_app(clock=FixedClock(START, START))
    seeded = seed(application)
    baseline = (
        state.items.copy(),
        state.ledger.copy(),
        list(state.events),
        state.commits,
    )
    state.fail_at = failure

    with pytest.raises(InternalFailure) as captured:
        application.design(CONTEXT, DesignCommand(seeded.service_id, manifest(), "design-1"))
    assert_bounded(captured.value)
    assert state.items == baseline[0]
    assert state.ledger == baseline[1]
    assert state.events == baseline[2]
    assert state.commits == baseline[3]


def test_rollback_failure_never_replaces_the_bounded_original_failure() -> None:
    application, state, _, _ = build_app()
    state.fail_at = "add"
    state.rollback_raises = True
    with pytest.raises(InternalFailure) as captured:
        seed(application)
    assert_bounded(captured.value)
    assert not state.items and not state.ledger and not state.events
    assert state.uows[-1].calls[-2:] == ["rollback", "close"]
    assert state.uows[-1].close_count == 1


def test_commit_failure_is_bounded_and_still_attempts_rollback_and_close() -> None:
    application, state, _, _ = build_app()
    state.fail_at = "commit"

    with pytest.raises(InternalFailure) as captured:
        seed(application)

    assert_bounded(captured.value)
    assert state.uows[-1].calls[-3:] == ["commit", "rollback", "close"]
    assert state.uows[-1].close_count == 1


def test_close_failure_after_commit_is_bounded_and_retry_replays_durable_result() -> None:
    application, state, _, _ = build_app()
    state.fail_at = "close"

    with pytest.raises(InternalFailure) as captured:
        seed(application)

    assert_bounded(captured.value)
    assert (len(state.items), len(state.events), state.commits) == (1, 1, 1)
    assert state.uows[-1].calls[-2:] == ["commit", "close"]
    assert state.uows[-1].close_count == 1

    state.fail_at = None
    replay = seed(application)
    assert len(state.items) == len(state.events) == state.commits == 1
    assert replay.service_id == next(iter(state.items))
    assert state.uows[-1].calls == ["claim", "close"]


def test_every_acquired_uow_is_closed_once_including_read_and_not_found_paths() -> None:
    application, state, _, _ = build_app()
    created = seed(application)
    seed(application)
    application.list(CONTEXT)
    application.inspect(CONTEXT, created.service_id)
    with pytest.raises(NotFound):
        application.inspect(CONTEXT, UUID(int=99))
    with pytest.raises(NotFound):
        application.design(CONTEXT, DesignCommand(UUID(int=99), manifest(), "missing-design"))

    assert state.uows
    assert all(uow.close_count == 1 for uow in state.uows)


@pytest.mark.parametrize("sentinel", HOSTILE_SENTINELS)
def test_adapter_failures_retain_no_hostile_exception_context(sentinel: str) -> None:
    application, state, _, _ = build_app()
    state.error_sentinel = sentinel
    state.fail_at = "claim"

    with pytest.raises(InternalFailure) as captured:
        seed(application)

    assert_bounded(captured.value, sentinel)


@pytest.mark.parametrize(
    "failure,operation", [("list", "list"), ("get", "inspect"), ("uow", "list")]
)
def test_read_failures_are_bounded(failure: str, operation: str) -> None:
    application, state, _, _ = build_app()
    state.fail_at = failure
    with pytest.raises(InternalFailure) as captured:
        if operation == "list":
            application.list(CONTEXT)
        else:
            application.inspect(CONTEXT, UUID(int=1))
    assert_bounded(captured.value)


def test_list_is_stable_bounded_and_defensively_projected() -> None:
    ids = SequentialIds(UUID(int=3), UUID(int=1), UUID(int=2))
    application, state, _, _ = build_app(ids=ids)
    for index in range(3):
        seed(application, key=f"seed-{index}", domain=f"notes_{index}")

    listed = application.list(CONTEXT, limit=2)
    assert [item.service_id for item in listed] == [UUID(int=1), UUID(int=2)]
    for item in listed:
        dumped = item.model_dump(mode="json", by_alias=True)
        assert "principal_id" not in dumped and "created_via" not in dumped
        assert PRINCIPAL not in str(dumped)
    assert state.uows[-1].calls == ["list", "close"]

    for invalid in (0, -1, 101, True, 1.0, "1"):
        with pytest.raises(InvalidCommand):
            application.list(CONTEXT, invalid)


@pytest.mark.parametrize(
    "context,command",
    [
        (ApplicationContext("", "test"), SeedCommand("notes", "project", "intent", "key")),
        (
            ApplicationContext(" principal", "test"),
            SeedCommand("notes", "project", "intent", "key"),
        ),
        (ApplicationContext("principal", ""), SeedCommand("notes", "project", "intent", "key")),
        (CONTEXT, SeedCommand("Notes", "project", "intent", "key")),
        (CONTEXT, SeedCommand("notes", "", "intent", "key")),
        (CONTEXT, SeedCommand("notes", "project", "", "key")),
        (CONTEXT, SeedCommand("notes", "project", "intent", "")),
    ],
)
def test_invalid_seed_input_is_rejected_before_a_uow(
    context: ApplicationContext,
    command: SeedCommand,
) -> None:
    application, state, _, _ = build_app()
    with pytest.raises(InvalidCommand):
        application.seed(context, command)
    assert state.uows == []


def test_invalid_injected_clock_and_identifier_are_bounded() -> None:
    naive_clock = FixedClock(datetime(2026, 1, 1))
    application, state, _, _ = build_app(clock=naive_clock)
    with pytest.raises(InternalFailure):
        seed(application)
    assert not state.items

    application, state, _, _ = build_app(ids=SequentialIds(UUID(int=0)))
    with pytest.raises(InternalFailure):
        seed(application)
    assert not state.items
