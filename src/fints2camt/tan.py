"""Strong customer authentication: waiting for a decoupled approval, or asking for a TAN.

An approver is called with the bank's challenge (a python-fints ``NeedTANResponse``) and
the client's ``send_tan``; it returns the bank's final response once the operation is
approved, or raises :class:`ApprovalPendingError`. ``run`` uses :class:`DecoupledWaiter`
(never prompts), ``setup`` and ``approve`` use :class:`InteractiveApprover`.

A TAN typed in by the operator goes to the bank only; it is never logged or printed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fints.client import NeedTANResponse

from .errors import ApprovalPendingError

log = logging.getLogger(__name__)

SendTan = Callable[[Any, str], Any]
Approver = Callable[[Any, SendTan], Any]
Prompt = Callable[[str], str]
Output = Callable[[str], None]

APPROVE_HINT = "approval needed – run `fints2camt approve`"


def is_challenge(response: Any) -> bool:
    return isinstance(response, NeedTANResponse)


@dataclass
class DecoupledWaiter:
    """Wait up to ``wait_seconds`` for an approval in the banking app (decoupled pushTAN).

    The challenge is never started again here: the bank's status is polled for the one
    challenge it sent. A challenge that needs a typed-in TAN cannot be answered without an
    operator and raises at once.
    """

    wait_seconds: float
    poll_seconds: float
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def __call__(self, challenge: Any, send_tan: SendTan) -> Any:
        if not challenge.decoupled:
            raise ApprovalPendingError(f"the bank asks for a TAN to be entered; {APPROVE_HINT}")
        log.info("the bank asks for an approval in the banking app; waiting up to %s", _duration(self.wait_seconds))
        deadline = self.clock() + self.wait_seconds
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ApprovalPendingError(f"the approval did not arrive within {_duration(self.wait_seconds)}")
            self.sleep(min(self.poll_seconds, remaining))
            response = send_tan(challenge, "")
            if not is_challenge(response):
                log.info("the approval arrived")
                return response
            challenge = response


@dataclass
class InteractiveApprover:
    """Show the challenge to the operator; wait for a decoupled approval or read the TAN."""

    wait_seconds: float
    poll_seconds: float
    prompt: Prompt = input
    out: Output = print
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def __call__(self, challenge: Any, send_tan: SendTan) -> Any:
        if challenge.challenge:
            self.out(f"The bank says: {challenge.challenge}")
        if challenge.decoupled:
            self.out(f"Approve the request in your banking app; waiting up to {_duration(self.wait_seconds)} ...")
            try:
                return DecoupledWaiter(self.wait_seconds, self.poll_seconds, self.sleep, self.clock)(challenge, send_tan)
            except ApprovalPendingError as exc:
                raise ApprovalPendingError(f"{exc}; run `fints2camt approve` to try again") from None
        if challenge.challenge_matrix:
            raise ApprovalPendingError(
                "this TAN method needs a graphic code, which a terminal cannot show; "
                "choose a decoupled method (pushTAN) with `fints2camt setup`"
            )
        if challenge.challenge_hhduc:
            self.out(f"Start code for the TAN generator: {challenge.challenge_hhduc}")
        tan = self.prompt("TAN: ").strip()
        if not tan:
            raise ApprovalPendingError("no TAN entered")
        response = send_tan(challenge, tan)
        if is_challenge(response):
            raise ApprovalPendingError("the bank did not accept the TAN")
        return response


def is_decoupled_method(parameters: Any) -> bool:
    """Whether a TAN method of the bank's parameter data is a decoupled one (pushTAN)."""
    if getattr(parameters, "decoupled_max_poll_number", None) is not None:
        return True
    zka_id = str(getattr(parameters, "zka_id", None) or "").lower()
    return "decoupled" in zka_id


def choose_tan_mechanism(mechanisms: Mapping[str, Any], prompt: Prompt = input, out: Output = print) -> str:
    """Let the operator pick a TAN method; a decoupled one is the default."""
    options = list(mechanisms.items())
    preferred = next((index for index, (_, p) in enumerate(options) if is_decoupled_method(p)), 0)
    if len(options) == 1:
        function, parameters = options[0]
        out(f"TAN method: {_mechanism_label(function, parameters)}")
        return function
    out("TAN methods offered by the bank:")
    for index, (function, parameters) in enumerate(options, start=1):
        out(f"  {index}) {_mechanism_label(function, parameters)}")
    index = _choose(len(options), preferred, "TAN method", prompt, out)
    return options[index][0]


def choose_tan_medium(media: Sequence[Any], prompt: Prompt = input, out: Output = print) -> Any:
    """Let the operator pick the TAN medium (e.g. the device for pushTAN)."""
    if len(media) == 1:
        return media[0]
    out("TAN media:")
    for index, medium in enumerate(media, start=1):
        out(f"  {index}) {medium.tan_medium_name}")
    return media[_choose(len(media), 0, "TAN medium", prompt, out)]


def _choose(count: int, default: int, what: str, prompt: Prompt, out: Output) -> int:
    while True:
        answer = prompt(f"{what} [{default + 1}]: ").strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        out(f"enter a number from 1 to {count}")


def _mechanism_label(function: str, parameters: Any) -> str:
    name = getattr(parameters, "name", None) or function
    return f"{name} ({function}, decoupled)" if is_decoupled_method(parameters) else f"{name} ({function})"


def _duration(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} seconds"
