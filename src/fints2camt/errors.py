"""Errors of fints2camt. Messages never contain the PIN, a TAN or a token."""

from __future__ import annotations

from typing import Any


class Fints2CamtError(Exception):
    """Base class of all errors raised by fints2camt."""


class ConfigError(Fints2CamtError):
    """The configuration file or a referenced secret is missing or invalid."""


class StateError(Fints2CamtError):
    """The state file cannot be read or written."""


class Mt940Error(Fints2CamtError):
    """The MT940 data from the bank cannot be parsed."""


class CamtError(Fints2CamtError):
    """The camt data from the bank cannot be read or converted."""


class CamtValidationError(CamtError):
    """The produced camt.053 does not validate against the XSD."""


class BankError(Fints2CamtError):
    """The bank refused or failed an operation."""


class WrongPinError(BankError):
    """The bank rejected the PIN. Never retry: repeated attempts lock the access."""


class LoginFailedError(BankError):
    """The bank refused the start of the dialog (its first answer, before the PIN was accepted).

    Banks answer a wrong PIN with various codes, not only the ones python-fints recognises,
    so ``run`` counts these failures in a row and stops contacting the bank after
    :data:`fints2camt.state.MAX_FAILED_LOGINS` of them, as for a rejected PIN.
    """


class AccountNotFoundError(BankError):
    """A configured account is not among the accounts the bank access can see."""


class TanRequiredError(BankError):
    """The bank asks for a TAN (strong customer authentication) to continue.

    ``challenge`` is the library object needed to complete or resume the approval.
    """

    def __init__(self, message: str, challenge: Any = None) -> None:
        super().__init__(message)
        self.challenge = challenge


class ApprovalPendingError(BankError):
    """The approval the bank asked for did not arrive in time, or needs an operator.

    ``run`` records it in the state and does not contact the bank again (no new push
    notification) until ``fints2camt approve`` succeeded.
    """


class AlreadyRunningError(Fints2CamtError):
    """Another fints2camt process holds the lock of the state file."""


class DeliveryError(Fints2CamtError):
    """A camt.053 could not be delivered to its target.

    ``status`` is the HTTP status of the last attempt, ``None`` for a network or file error.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
