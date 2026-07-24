"""Backend-neutral values for logical service-store transfer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from aipcs_mcp.records import (
    BranchValue,
    RecordHistoryEvent,
    RecordSpecification,
    RecordValue,
)

MAX_FENCE_GENERATION = 2**63 - 1
MAX_STORE_MEMBERS = 1_000_000 - 3

FenceState = Literal["open", "closed"]
PortableMemberKind = Literal["record", "history", "branch", "branch_membership"]
StageStatus = Literal["staged", "already_staged", "different"]
FenceTransitionStatus = Literal["applied", "current", "stale"]
PurgeStatus = Literal["purged", "already_absent"]


class PortableStoreContractError(ValueError):
    """One bounded failure for an invalid logical transfer value."""

    def __init__(self) -> None:
        super().__init__("The portable service-store value is invalid.")


@dataclass(frozen=True, slots=True)
class WriteAdmissionFence:
    """Service-local monotonic write-admission evidence."""

    generation: int
    state: FenceState

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or not 1 <= self.generation <= MAX_FENCE_GENERATION
            or type(self.state) is not str
            or self.state not in {"open", "closed"}
        ):
            raise PortableStoreContractError()


@dataclass(frozen=True, slots=True)
class BranchMembership:
    """One logical branch-to-record relationship; physical assignment time is excluded."""

    branch_id: UUID
    entity_name: str
    record_id: UUID
    role: Literal["primary", "related"]

    def __post_init__(self) -> None:
        if (
            type(self.branch_id) is not UUID
            or self.branch_id.int == 0
            or type(self.entity_name) is not str
            or not self.entity_name
            or len(self.entity_name) > 64
            or type(self.record_id) is not UUID
            or self.record_id.int == 0
            or type(self.role) is not str
            or self.role not in {"primary", "related"}
        ):
            raise PortableStoreContractError()


PortableMemberValue = RecordValue | RecordHistoryEvent | BranchValue | BranchMembership


@dataclass(frozen=True, slots=True)
class PortableStoreMember:
    """A detached member tagged with its canonical transfer section."""

    kind: PortableMemberKind
    value: PortableMemberValue

    def __post_init__(self) -> None:
        expected = {
            "record": RecordValue,
            "history": RecordHistoryEvent,
            "branch": BranchValue,
            "branch_membership": BranchMembership,
        }
        if type(self.kind) is not str or self.kind not in expected or type(self.value) is not expected[
            self.kind
        ]:
            raise PortableStoreContractError()


@dataclass(frozen=True, slots=True)
class StageResult:
    status: StageStatus
    fence: WriteAdmissionFence

    def __post_init__(self) -> None:
        if (
            type(self.status) is not str
            or self.status not in {"staged", "already_staged", "different"}
            or type(self.fence) is not WriteAdmissionFence
        ):
            raise PortableStoreContractError()


@dataclass(frozen=True, slots=True)
class FenceTransitionResult:
    status: FenceTransitionStatus
    fence: WriteAdmissionFence

    def __post_init__(self) -> None:
        if (
            type(self.status) is not str
            or self.status not in {"applied", "current", "stale"}
            or type(self.fence) is not WriteAdmissionFence
        ):
            raise PortableStoreContractError()


@dataclass(frozen=True, slots=True)
class PurgeStoreResult:
    status: PurgeStatus

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in {
            "purged",
            "already_absent",
        }:
            raise PortableStoreContractError()


def portable_member_sort_key(member: PortableStoreMember) -> tuple[object, ...]:
    """Return the single canonical member order used by both reference adapters."""

    if type(member) is not PortableStoreMember:
        raise PortableStoreContractError()
    value = member.value
    section = {
        "record": 0,
        "history": 1,
        "branch": 2,
        "branch_membership": 3,
    }[member.kind]
    if type(value) is RecordValue:
        return section, value.entity_name, value.record_id.hex
    if type(value) is RecordHistoryEvent:
        return section, value.entity_name, value.record_id.hex, value.record_version
    if type(value) is BranchValue:
        return section, value.branch_id.hex
    assert type(value) is BranchMembership
    return section, value.branch_id.hex, value.entity_name, value.record_id.hex, value.role


def validate_portable_members(
    members: Iterable[PortableStoreMember], specification: RecordSpecification
) -> tuple[PortableStoreMember, ...]:
    """Detach and validate one complete logical store in canonical order."""

    try:
        if not isinstance(members, Iterable) or type(specification) is not RecordSpecification:
            raise ValueError
        values = tuple(members)
        if len(values) > MAX_STORE_MEMBERS or any(
            type(value) is not PortableStoreMember for value in values
        ):
            raise ValueError
        keys = tuple(portable_member_sort_key(value) for value in values)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError

        records: dict[tuple[str, UUID], RecordValue] = {}
        histories: dict[tuple[str, UUID], list[RecordHistoryEvent]] = {}
        branches: dict[UUID, BranchValue] = {}
        memberships: list[BranchMembership] = []
        for member in values:
            value = member.value
            if type(value) is RecordValue:
                checked = specification.validate_create(value.entity_name, value.fields)
                if checked != value.fields:
                    raise ValueError
                records[(value.entity_name, value.record_id)] = value
            elif type(value) is RecordHistoryEvent:
                if specification.entity(value.entity_name) is None:
                    raise ValueError
                for snapshot in (value.before, value.after):
                    if snapshot is not None and specification.validate_create(
                        value.entity_name, snapshot
                    ) != snapshot:
                        raise ValueError
                histories.setdefault((value.entity_name, value.record_id), []).append(value)
            elif type(value) is BranchValue:
                branches[value.branch_id] = value
            else:
                assert type(value) is BranchMembership
                memberships.append(value)

        for identity, events in histories.items():
            versions = tuple(event.record_version for event in events)
            if (
                versions != tuple(range(1, len(events) + 1))
                or events[0].kind != "create"
                or any(event.kind == "create" for event in events[1:])
                or any(event.kind == "delete" for event in events[:-1])
            ):
                raise ValueError
            current = records.get(identity)
            if (events[-1].kind == "delete") != (current is None) or (
                current is not None and current.record_version != events[-1].record_version
            ):
                raise ValueError
        if any(identity not in histories for identity in records):
            raise ValueError

        for branch in branches.values():
            if branch.parent_branch_id is not None and branch.parent_branch_id not in branches:
                raise ValueError
            seen: set[UUID] = set()
            parent = branch.parent_branch_id
            while parent is not None:
                if parent in seen or parent == branch.branch_id:
                    raise ValueError
                seen.add(parent)
                parent = branches[parent].parent_branch_id

        relationship_keys: set[tuple[UUID, str, UUID]] = set()
        primary_keys: set[tuple[str, UUID]] = set()
        for membership in memberships:
            identity = (membership.entity_name, membership.record_id)
            relationship = (membership.branch_id, *identity)
            if (
                membership.branch_id not in branches
                or identity not in records
                or relationship in relationship_keys
                or (
                    membership.role == "primary"
                    and identity in primary_keys
                )
            ):
                raise ValueError
            relationship_keys.add(relationship)
            if membership.role == "primary":
                primary_keys.add(identity)
        return values
    except Exception:
        raise PortableStoreContractError() from None
