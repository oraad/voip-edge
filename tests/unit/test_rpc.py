"""Unit tests for console.rpc — JSON-RPC 2.0 over a unix datagram socket."""
from __future__ import annotations

import time

import pytest

from console.errors import ConsoleError, KamailioUnavailable
from console.rpc import RPCFault, UnixDatagramRPC, flatten_htable_dump
from tests.fixtures.fakerpc import FakeRPC


@pytest.fixture()
def fake(tmp_path):
    with FakeRPC(tmp_path / "kamailio_rpc.sock") as server:
        yield server


@pytest.fixture()
def rpc(fake):
    return UnixDatagramRPC(fake.sock_path, timeout_ms=250)


def test_round_trip_dict_result(fake, rpc):
    fake.script("core.echo", {"ok": True, "nested": [1, 2, 3]})

    assert rpc.call("core.echo", [{"hello": "world"}]) == {"ok": True, "nested": [1, 2, 3]}
    assert fake.call_count == 1
    call = fake.calls[0]
    assert call.method == "core.echo"
    assert call.params == [{"hello": "world"}]


def test_round_trip_list_and_str_results(fake, rpc):
    fake.script("htable.dump", [{"entry": 0, "size": 0, "slot": []}])
    fake.script("core.version", "kamailio 6.1.5")

    assert rpc.call("htable.dump", ["regcache"]) == [{"entry": 0, "size": 0, "slot": []}]
    assert rpc.call("core.version") == "kamailio 6.1.5"


def test_id_counter_increments(fake, rpc):
    fake.script("core.version", "ok")

    rpc.call("core.version")
    rpc.call("core.version")

    assert fake.request_ids == [1, 2]


def test_timeout_raises_kamailio_unavailable_fast(fake):
    fake.silent("slow.method")
    rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=50)

    start = time.monotonic()
    with pytest.raises(KamailioUnavailable):
        rpc.call("slow.method", [])
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"timeout took {elapsed:.3f}s"


def test_jsonrpc_error_raises_rpcfault_with_code_and_message(fake, rpc):
    fake.fault("htable.reload", code=-1, message="cannot reload htable homes")

    with pytest.raises(RPCFault) as excinfo:
        rpc.call("htable.reload", ["homes"])

    fault = excinfo.value
    assert isinstance(fault, ConsoleError)
    assert fault.method == "htable.reload"
    assert fault.code == -1
    assert fault.message == "cannot reload htable homes"
    assert "cannot reload htable homes" in str(fault)


def test_missing_socket_path_raises_kamailio_unavailable(tmp_path):
    rpc = UnixDatagramRPC(str(tmp_path / "does-not-exist.sock"), timeout_ms=50)

    with pytest.raises(KamailioUnavailable):
        rpc.call("core.version")


def test_garbled_reply_raises_kamailio_unavailable(fake, rpc):
    fake.raw("broken.method", b"\xff\xfe not json at all")

    with pytest.raises(KamailioUnavailable):
        rpc.call("broken.method")


def test_short_truncated_reply_raises_kamailio_unavailable(fake, rpc):
    fake.raw("short.method", b'{"jsonrpc": "2.0", "id": 1, "result":')

    with pytest.raises(KamailioUnavailable):
        rpc.call("short.method")


def test_non_object_reply_raises_kamailio_unavailable(fake, rpc):
    fake.raw("list.reply", b"[1, 2, 3]")

    with pytest.raises(KamailioUnavailable):
        rpc.call("list.reply")


def test_reply_without_result_or_error_raises_kamailio_unavailable(fake, rpc):
    fake.raw("empty.reply", b'{"jsonrpc": "2.0", "id": 1}')

    with pytest.raises(KamailioUnavailable):
        rpc.call("empty.reply")


# -- flatten_htable_dump ---------------------------------------------------


def test_flatten_multi_item_bucket_list():
    dump = [
        {
            "entry": 0,
            "size": 2,
            "slot": [
                {"item": {"name": "1000@a.example", "value": "sip:1000@10.0.0.1", "expires": 1740}},
                {"item": {"name": "1001@a.example", "value": "sip:1001@10.0.0.2"}},
            ],
        }
    ]

    assert flatten_htable_dump(dump) == {
        "1000@a.example": {"value": "sip:1000@10.0.0.1", "expires": 1740},
        "1001@a.example": {"value": "sip:1001@10.0.0.2", "expires": None},
    }


def test_flatten_single_item_dict_slot():
    dump = [
        {"entry": 7, "size": 1, "slot": {"item": {"name": "a@x.example", "value": "v"}}},
    ]

    assert flatten_htable_dump(dump) == {
        "a@x.example": {"value": "v", "expires": None},
    }


def test_flatten_item_dict_without_wrapper():
    dump = [
        {"entry": 3, "size": 1, "slot": {"name": "b@x.example", "value": "w", "expires": 9}},
    ]

    assert flatten_htable_dump(dump) == {
        "b@x.example": {"value": "w", "expires": 9},
    }


def test_flatten_integer_value_is_stringified():
    dump = [
        {
            "entry": 1,
            "size": 2,
            "slot": [
                {"item": {"name": "counter", "value": 42, "expires": 0}},
                {"item": {"name": "flag", "value": 0}},
            ],
        }
    ]

    assert flatten_htable_dump(dump) == {
        "counter": {"value": "42", "expires": 0},
        "flag": {"value": "0", "expires": None},
    }


def test_flatten_empty_and_none():
    assert flatten_htable_dump([]) == {}
    assert flatten_htable_dump(None) == {}
