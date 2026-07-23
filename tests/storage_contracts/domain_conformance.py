"""Backend-neutral materialisation and additive-evolution parity cases."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from aipcs_mcp.relational import (
    RelationalSpecification,
    RelationalTransition,
    classify_transition,
    compile_manifest,
)
from aipcs_mcp.storage import DomainSchemaState

from .parity_fixtures import evolved_parity_manifest, parity_manifest


class DomainParityHarness(Protocol):
    """Test-only boundary a backend supplies without exposing its storage details."""

    def materialise(self, specification: RelationalSpecification) -> DomainSchemaState: ...

    def inspect(self, specification: RelationalSpecification) -> DomainSchemaState: ...

    def evolve(self, transition: RelationalTransition) -> DomainSchemaState: ...


DomainParityHarnessFactory = Callable[[], DomainParityHarness]


def assert_domain_materialisation_and_evolution(factory: DomainParityHarnessFactory) -> None:
    """Assert idempotent materialisation and one additive evolution in public terms."""

    current_manifest = parity_manifest()
    target_manifest = evolved_parity_manifest()
    current = compile_manifest(current_manifest)
    target = compile_manifest(target_manifest)
    transition = classify_transition(current_manifest, target_manifest)
    harness = factory()

    assert harness.inspect(current) == DomainSchemaState("unmaterialised")
    assert harness.materialise(current) == DomainSchemaState("ready")
    assert harness.materialise(current) == DomainSchemaState("ready")
    assert harness.inspect(current) == DomainSchemaState("ready")
    assert harness.evolve(transition) == DomainSchemaState("ready")
    assert harness.evolve(transition) == DomainSchemaState("ready")
    assert harness.inspect(target) == DomainSchemaState("ready")
    assert harness.inspect(current) == DomainSchemaState("incompatible")
