from types import SimpleNamespace

import pytest

from fints2camt.errors import ApprovalPendingError
from fints2camt.tan import DecoupledWaiter, InteractiveApprover, choose_tan_mechanism, choose_tan_medium

from fake_fints import mechanism, tan_challenge


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Bank:
    """send_tan: answers 'not yet' ``pending`` times, then the operation's response."""

    def __init__(self, pending: int, response: object = "response") -> None:
        self.pending = pending
        self.response = response
        self.calls: list[tuple[object, str]] = []

    def send_tan(self, challenge, tan):
        self.calls.append((challenge, tan))
        if len(self.calls) <= self.pending:
            return tan_challenge()
        return self.response


def test_decoupled_approval_arrives_while_waiting() -> None:
    time, bank = FakeTime(), Bank(pending=2)
    waiter = DecoupledWaiter(300, 5, time.sleep, time.clock)
    assert waiter(tan_challenge(), bank.send_tan) == "response"
    assert len(bank.calls) == 3
    assert all(tan == "" for _, tan in bank.calls)
    assert time.sleeps == [5, 5, 5]


def test_decoupled_wait_ends_after_the_configured_time() -> None:
    time, bank = FakeTime(), Bank(pending=10**6)
    waiter = DecoupledWaiter(300, 7, time.sleep, time.clock)
    with pytest.raises(ApprovalPendingError, match="5 minutes"):
        waiter(tan_challenge(), bank.send_tan)
    assert time.now == 300
    assert len(bank.calls) == 43  # status queries for the one challenge, ending at the deadline


def test_no_wait_configured_gives_up_at_once() -> None:
    time, bank = FakeTime(), Bank(pending=0)
    with pytest.raises(ApprovalPendingError):
        DecoupledWaiter(0, 5, time.sleep, time.clock)(tan_challenge(), bank.send_tan)
    assert bank.calls == []


def test_a_typed_tan_cannot_be_answered_unattended() -> None:
    bank = Bank(pending=0)
    with pytest.raises(ApprovalPendingError, match="fints2camt approve"):
        DecoupledWaiter(300, 5)(tan_challenge(decoupled=False), bank.send_tan)
    assert bank.calls == []


def interactive(answers: list[str], time: FakeTime | None = None) -> tuple[InteractiveApprover, list[str]]:
    output: list[str] = []
    time = time or FakeTime()
    approver = InteractiveApprover(600, 5, prompt=lambda _: answers.pop(0), out=output.append, sleep=time.sleep, clock=time.clock)
    return approver, output


def test_interactive_decoupled_shows_the_challenge_and_waits() -> None:
    approver, output = interactive([])
    bank = Bank(pending=1)
    assert approver(tan_challenge(text="Approve login"), bank.send_tan) == "response"
    assert output[0] == "The bank says: Approve login"
    assert "banking app" in output[1]


def test_interactive_decoupled_timeout() -> None:
    approver, _ = interactive([])
    with pytest.raises(ApprovalPendingError, match="try again"):
        approver(tan_challenge(), Bank(pending=10**6).send_tan)


def test_interactive_typed_tan_goes_to_the_bank_and_is_not_printed() -> None:
    approver, output = interactive([" 123456 "])
    bank = Bank(pending=0)
    assert approver(tan_challenge(decoupled=False), bank.send_tan) == "response"
    assert bank.calls[0][1] == "123456"
    assert not any("123456" in line for line in output)


def test_interactive_empty_or_rejected_tan() -> None:
    approver, _ = interactive([""])
    with pytest.raises(ApprovalPendingError, match="no TAN"):
        approver(tan_challenge(decoupled=False), Bank(pending=0).send_tan)
    approver, _ = interactive(["111111"])
    with pytest.raises(ApprovalPendingError, match="did not accept"):
        approver(tan_challenge(decoupled=False), Bank(pending=1).send_tan)


def test_interactive_graphic_challenge_is_refused() -> None:
    approver, _ = interactive([])
    with pytest.raises(ApprovalPendingError, match="pushTAN"):
        approver(tan_challenge(decoupled=False, matrix=("image/png", b"x")), Bank(pending=0).send_tan)


def test_decoupled_method_is_the_default_choice() -> None:
    mechanisms = {"910": mechanism("chipTAN", False), "921": mechanism("pushTAN 2.0", True)}
    output: list[str] = []
    questions: list[str] = []
    assert choose_tan_mechanism(mechanisms, prompt=lambda q: questions.append(q) or "", out=output.append) == "921"
    assert questions == ["TAN method [2]: "]
    assert "pushTAN 2.0 (921, decoupled)" in output[2]
    answers = ["9", "x", "1"]
    assert choose_tan_mechanism(mechanisms, prompt=lambda q: answers.pop(0), out=output.append) == "910"
    assert output.count("enter a number from 1 to 2") == 2


def test_single_method_and_medium_need_no_choice() -> None:
    def no_prompt(_: str) -> str:
        raise AssertionError("no question expected")

    assert choose_tan_mechanism({"921": mechanism("pushTAN", True)}, no_prompt, lambda _: None) == "921"
    phone = SimpleNamespace(tan_medium_name="Phone")
    assert choose_tan_medium([phone], no_prompt, lambda _: None) is phone
    tablet = SimpleNamespace(tan_medium_name="Tablet")
    assert choose_tan_medium([phone, tablet], lambda _: "2", lambda _: None) is tablet
