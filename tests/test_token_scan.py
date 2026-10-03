"""The token check's scan of nested values, under either protobuf backend.

The default backend is upb. `test_the_scan_holds_under_the_pure_python_backend` runs this
module again with `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`, since the two backends
build messages, repeated fields and maps differently.
"""

import os
import subprocess
import sys
import traceback

import pytest
from google.protobuf import struct_pb2
from google.protobuf.internal import api_implementation
from test_session import assert_token_absent, synthetic_token

from qte_sdk.contract.v1.session_pb2 import Calendar, Holiday, Subscribe
from qte_sdk.session import _holds_token, _Secret, _token_forms

# Set for the run under the pure-python backend, so that run can check it got it.
BACKEND_VAR = "QTE_TEST_PROTOBUF_BACKEND"


@pytest.mark.skipif(BACKEND_VAR not in os.environ, reason="checked in the run under python")
def test_the_backend_is_the_one_asked_for():
    assert api_implementation.Type() == os.environ[BACKEND_VAR]


def tree(token: str | None, at: tuple[int, int]) -> dict:
    """Twelve maps of twelve strings each, with `token`, if given, at `at`."""
    values = {f"k{i}": {f"j{j}": f"v{i}-{j}" for j in range(12)} for i in range(12)}
    if token is not None:
        values[f"k{at[0]}"][f"j{at[1]}"] = token
    return values


def test_a_token_in_a_struct_inside_a_map_is_found_wherever_it_is():
    # A Struct is a map of Values, and each nested Struct another map. Under upb each
    # access makes a new wrapper, so a scan that kept only ids skipped most of them.
    token = synthetic_token()
    secret = _Secret(token)
    placements = [(i, j) for i in range(12) for j in range(12)]
    missed = []
    for at in placements:
        message = struct_pb2.Struct()
        message.update(tree(token, at))
        if not _holds_token(message, secret):
            missed.append(at)
    assert missed == []
    clean = struct_pb2.Struct()
    clean.update(tree(None, (0, 0)))
    assert not _holds_token(clean, secret)


def test_a_token_in_a_repeated_field_is_found():
    token = synthetic_token()
    secret = _Secret(token)
    assert _holds_token(Subscribe(instruments=["AAPL", token]), secret)
    assert not _holds_token(Subscribe(instruments=["AAPL", "MSFT"]), secret)
    holidays = [Holiday(date="2027-06-16", name="A holiday"), Holiday(date="2027-06-18")]
    assert not _holds_token(Calendar(holidays=holidays), secret)
    holidays[1].name = token
    assert _holds_token(Calendar(holidays=holidays), secret)


class Interrupt(BaseException):
    """Stands in for a Ctrl-C arriving while the scan runs."""


class Exploding(Exception):
    def __str__(self) -> str:
        raise Interrupt

    def __repr__(self) -> str:
        return "Exploding()"


@pytest.mark.parametrize("special", ["", "\\", "\n"], ids=["plain", "backslash", "newline"])
def test_a_scan_cut_short_leaves_no_form_of_the_token_in_a_traceback(special: str):
    token = synthetic_token() + special + synthetic_token()
    secret = _Secret(token)
    forms = [form.value for form in _token_forms(secret)]
    with pytest.raises(Interrupt) as caught:
        _holds_token([Exploding()], secret)
    rendered = traceback.TracebackException.from_exception(caught.value, capture_locals=True)
    # This module's own frames hold the token by design.
    frames = [frame for frame in rendered.stack if frame.filename != __file__]
    assert [frame.name for frame in frames] == ["_holds_token"]
    text = "\n".join(f"{frame.name} {frame.locals}" for frame in frames)
    assert_token_absent(token, text)
    for form in forms:
        assert form not in text


def test_the_forms_of_the_token_are_held_withheld():
    token = synthetic_token() + "\\" + synthetic_token()
    forms = _token_forms(_Secret(token))
    assert len(forms) > 1
    assert_token_absent(token, repr(forms) + str(forms))


@pytest.mark.skipif(BACKEND_VAR in os.environ, reason="this is the run it starts")
def test_the_scan_holds_under_the_pure_python_backend():
    env = {
        **os.environ,
        "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
        BACKEND_VAR: "python",
    }
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", __file__],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    assert "1 skipped" in run.stdout
