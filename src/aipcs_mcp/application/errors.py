"""Transport-neutral application errors."""

from __future__ import annotations


class ApplicationError(Exception):
    code = "invalid_state"
    message = "The requested application operation is not valid."

    def __init__(self) -> None:
        super().__init__(self.message)


class NotFound(ApplicationError):
    code = "not_found"
    message = "The requested service was not found."


class Conflict(ApplicationError):
    code = "conflict"
    message = "The request conflicts with existing application state."


class InvalidState(ApplicationError):
    code = "invalid_state"
    message = "The service is not in a state that permits this operation."


class InvalidCommand(ApplicationError):
    code = "validation_failed"
    message = "The application command is invalid."


class InternalFailure(ApplicationError):
    code = "internal_error"
    message = "The application operation could not be completed."
