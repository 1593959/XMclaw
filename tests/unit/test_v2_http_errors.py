"""vendor_error_message / raise_for_vendor_error — surface vendor API
error bodies instead of bare status codes.

2026-06-17. Regression: a Volcengine Ark video POST 404'd with
``UnsupportedModel: ... does not support the agent plan feature`` but the
agent only saw "(404)" and fabricated a fake animation. The message must
ride through.
"""
from __future__ import annotations

import pytest

from xmclaw.utils.http_errors import (
    raise_for_vendor_error,
    vendor_error_message,
)


class _Resp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_openai_shape_error_with_code() -> None:
    r = _Resp(404, {"error": {"code": "UnsupportedModel",
                              "message": "does not support the agent plan feature"}})
    assert vendor_error_message(r) == (
        "UnsupportedModel: does not support the agent plan feature"
    )


def test_flat_message_field() -> None:
    assert vendor_error_message(_Resp(400, {"message": "bad prompt"})) == "bad prompt"


def test_minimax_base_resp_envelope() -> None:
    r = _Resp(200, {"base_resp": {"status_code": 1004, "status_msg": "auth failed"}})
    assert vendor_error_message(r) == "1004: auth failed"


def test_falls_back_to_raw_text_when_not_json() -> None:
    assert vendor_error_message(_Resp(502, None, text="Bad Gateway")) == "Bad Gateway"


def test_raise_includes_status_and_message() -> None:
    r = _Resp(404, {"error": {"code": "UnsupportedModel", "message": "nope"}})
    with pytest.raises(RuntimeError) as ei:
        raise_for_vendor_error(r, "Ark create video task (model=doubao-seedance-2.0)")
    msg = str(ei.value)
    assert "Ark create video task" in msg
    assert "404" in msg
    assert "UnsupportedModel" in msg and "nope" in msg


def test_no_raise_on_success() -> None:
    raise_for_vendor_error(_Resp(200, {"id": "ok"}), "create")  # no exception
