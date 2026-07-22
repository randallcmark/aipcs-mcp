"""SYNTHETIC_FIXTURE. Backend-neutral domain-schema store contract proof."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from inspect import Parameter, signature
from typing import cast, get_args, get_type_hints
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import (
    RelationalSpecification,
    RelationalTransition,
    classify_transition,
)
from aipcs_mcp.storage import (
    DomainSchemaState,
    DomainSchemaStatus,
    DomainSchemaStore,
    ServiceStoreLocator,
    StorageContractError,
)


class _TextSubclass(str):
    pass


class ContractDomainSchemaStore:
    """Deterministic test-only fake; it has no adapter or persistence mechanics."""

    def __init__(self) -> None:
        self._schemas: dict[ServiceStoreLocator, RelationalSpecification] = {}
        self.calls: list[str] = []

    def inspect(
        self,
        locator: ServiceStoreLocator,
        specification: RelationalSpecification,
    ) -> DomainSchemaState:
        self.calls.append("inspect")
        current = self._schemas.get(locator)
        if current is None:
            return DomainSchemaState("unmaterialised")
        return DomainSchemaState("ready" if current == specification else "incompatible")

    def materialise(
        self,
        locator: ServiceStoreLocator,
        specification: RelationalSpecification,
    ) -> DomainSchemaState:
        self.calls.append("materialise")
        current = self._schemas.get(locator)
        if current is not None and current != specification:
            return DomainSchemaState("incompatible")
        self._schemas[locator] = specification
        return DomainSchemaState("ready")

    def evolve(
        self,
        locator: ServiceStoreLocator,
        transition: RelationalTransition,
    ) -> DomainSchemaState:
        self.calls.append("evolve")
        current = self._schemas.get(locator)
        if current == transition.target:
            return DomainSchemaState("ready")
        if current != transition.current:
            return DomainSchemaState("incompatible")
        self._schemas[locator] = transition.target
        return DomainSchemaState("ready")


def _transition() -> RelationalTransition:
    current = valid_manifest()
    target = deepcopy(current)
    target["schema_version"] = 2
    target["migration_history"] = [
        {
            "from_schema_version": 1,
            "to_schema_version": 2,
            "operations": ["add project title index"],
        }
    ]
    target["indices"].append(
        {
            "name": "project_title_idx",
            "entity": "project",
            "fields": ["title"],
        }
    )
    return classify_transition(
        ManifestV2.model_validate(current),
        ManifestV2.model_validate(target),
    )


def test_domain_schema_status_and_state_are_closed_exact_values() -> None:
    assert get_args(DomainSchemaStatus) == ("unmaterialised", "ready", "incompatible")
    assert [field.name for field in fields(DomainSchemaState)] == ["status"]

    for status in get_args(DomainSchemaStatus):
        state = DomainSchemaState(status)
        assert state.status == status
        assert hash(state) == hash(DomainSchemaState(status))
        with pytest.raises(FrozenInstanceError):
            state.status = "ready"  # type: ignore[misc]

    for invalid in ("uninitialised", "dirty", "materialised", "", 1, True, None):
        with pytest.raises(StorageContractError) as captured:
            DomainSchemaState(cast(DomainSchemaStatus, invalid))
        assert captured.value.args == (StorageContractError.message,)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    with pytest.raises(StorageContractError):
        DomainSchemaState(cast(DomainSchemaStatus, _TextSubclass("ready")))


@pytest.mark.parametrize(
    ("method_name", "parameter_names", "type_hints"),
    [
        (
            "inspect",
            ["self", "locator", "specification"],
            {
                "locator": ServiceStoreLocator,
                "specification": RelationalSpecification,
                "return": DomainSchemaState,
            },
        ),
        (
            "materialise",
            ["self", "locator", "specification"],
            {
                "locator": ServiceStoreLocator,
                "specification": RelationalSpecification,
                "return": DomainSchemaState,
            },
        ),
        (
            "evolve",
            ["self", "locator", "transition"],
            {
                "locator": ServiceStoreLocator,
                "transition": RelationalTransition,
                "return": DomainSchemaState,
            },
        ),
    ],
)
def test_domain_schema_store_has_exact_backend_neutral_signatures(
    method_name: str,
    parameter_names: list[str],
    type_hints: dict[str, object],
) -> None:
    method = getattr(DomainSchemaStore, method_name)
    parameters = list(signature(method).parameters.values())
    assert [parameter.name for parameter in parameters] == parameter_names
    assert all(parameter.kind is Parameter.POSITIONAL_OR_KEYWORD for parameter in parameters)
    assert all(parameter.default is Parameter.empty for parameter in parameters)
    assert get_type_hints(method) == type_hints

    protocol_methods = {
        name
        for name, value in vars(DomainSchemaStore).items()
        if not name.startswith("_") and callable(value)
    }
    assert protocol_methods == {"inspect", "materialise", "evolve"}


def test_deterministic_fake_satisfies_relative_schema_contract() -> None:
    store: DomainSchemaStore = ContractDomainSchemaStore()
    fake = cast(ContractDomainSchemaStore, store)
    first = ServiceStoreLocator.for_service("sqlite", UUID(int=1))
    second = ServiceStoreLocator.for_service("sqlite", UUID(int=2))
    transition = _transition()

    assert store.inspect(first, transition.current) == DomainSchemaState("unmaterialised")
    assert store.materialise(first, transition.current) == DomainSchemaState("ready")
    assert store.materialise(first, transition.current) == DomainSchemaState("ready")
    assert store.inspect(first, transition.current) == DomainSchemaState("ready")
    assert store.inspect(second, transition.current) == DomainSchemaState("unmaterialised")

    assert store.evolve(first, transition) == DomainSchemaState("ready")
    assert store.evolve(first, transition) == DomainSchemaState("ready")
    assert store.inspect(first, transition.target) == DomainSchemaState("ready")
    assert store.inspect(first, transition.current) == DomainSchemaState("incompatible")
    assert store.materialise(first, transition.current) == DomainSchemaState("incompatible")
    assert fake.calls == [
        "inspect",
        "materialise",
        "materialise",
        "inspect",
        "inspect",
        "evolve",
        "evolve",
        "inspect",
        "inspect",
        "materialise",
    ]


def test_public_storage_package_exports_only_the_schema_contract_values() -> None:
    import aipcs_mcp.storage as storage

    assert storage.DomainSchemaStatus is DomainSchemaStatus
    assert storage.DomainSchemaState is DomainSchemaState
    assert storage.DomainSchemaStore is DomainSchemaStore
    assert not any(
        name in vars(DomainSchemaState)
        for name in ("schema_version", "fingerprint", "history", "path", "dsn", "sql")
    )
