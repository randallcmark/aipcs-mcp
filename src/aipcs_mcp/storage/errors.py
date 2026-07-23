"""Bounded storage errors that never retain adapter implementation details."""

from __future__ import annotations


class StorageContractError(ValueError):
    """Invalid pure-contract value with no reflected rejected input."""

    message = "The storage contract value is invalid."

    def __init__(self) -> None:
        super().__init__(self.message)


class StorageError(Exception):
    """Base error for future adapter boundaries with a stable safe message."""

    message = "The storage operation could not be completed."

    def __init__(self) -> None:
        super().__init__(self.message)


class StorageUnavailable(StorageError):
    message = "The requested storage is unavailable."


class StorageTransientJournal(StorageUnavailable):
    """A safe rollback journal blocks read-only inspection during migration."""


class StorageBusy(StorageError):
    """The adapter reported bounded, retryable storage contention."""

    message = "The requested storage is busy."


class StorageMigrationError(StorageError):
    message = "The storage migration state is incompatible."
