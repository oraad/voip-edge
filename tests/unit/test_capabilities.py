"""T9 unit tests — capability probe, cache and the refuse-before-purge helper."""
from __future__ import annotations

import pytest

from console import capabilities
from console.capabilities import (
    CAPABILITY_KEYS,
    clear_cache,
    missing_capabilities,
    probe,
    require_capability,
)
from console.errors import KamailioUnavailable
from console.rpc import UnixDatagramRPC
from tests.fixtures.fakerpc import FakeRPC

RELOAD_METHODS = (
    "mtree.reload",
    "dispatcher.reload",
    "dialplan.reload",
    "permissions.trustedReload",
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture()
def fake(tmp_path):
    with FakeRPC(tmp_path / "kamailio_rpc.sock", timeout_ms=200) as server:
        yield server


def script_healthy(fake: FakeRPC) -> None:
    fake.script("core.uptime", {"uptime": "6 days, 3 hours"})
    fake.script("htable.dump", [])
    fake.script("htable.delete", 1)
    for method in RELOAD_METHODS:
        fake.script(method, 1)


def test_probe_ok_marks_every_capability_and_aggregate(fake):
    script_healthy(fake)

    result = probe(fake)

    for key in CAPABILITY_KEYS:
        assert result[key] is True, key
    assert result["jsonrpcs"] is True
    assert result["reachable"] is True
    assert result["unknown"] is False
    assert result["ok"] is True
    assert result["aggregate"] is True
    assert capabilities.last_uptime() == "6 days, 3 hours"
    assert missing_capabilities(result) == []


def test_probe_fault_marks_only_that_capability_false(fake):
    script_healthy(fake)
    fake.fault("htable.delete", code=500, message="delete not available")

    result = probe(fake)

    assert result["htable.delete"] is False
    assert result["core.uptime"] is True
    assert result["htable.dump"] is True
    assert result["reachable"] is True
    assert result["unknown"] is False
    assert result["ok"] is False
    assert "htable.delete" in missing_capabilities(result)


def test_probe_missing_socket_degrades_without_raising(tmp_path):
    rpc = UnixDatagramRPC(str(tmp_path / "does-not-exist.sock"), timeout_ms=20)

    result = probe(rpc)

    assert result["reachable"] is False
    assert result["unknown"] is True
    assert result["ok"] is False
    assert all(result[key] is False for key in CAPABILITY_KEYS)
    # One transport failure short-circuits the remaining calls.
    assert capabilities.last_uptime() is None


def test_probe_is_cached_within_ttl_and_refreshes_after_clear(fake):
    script_healthy(fake)

    first = probe(fake, ttl=60)
    calls_after_first = fake.call_count
    second = probe(fake, ttl=60)

    assert second == first
    assert fake.call_count == calls_after_first, "second probe must use the cache"

    clear_cache()
    probe(fake, ttl=60)
    assert fake.call_count > calls_after_first


def test_probe_zero_ttl_always_calls(fake):
    script_healthy(fake)

    probe(fake, ttl=0)
    first = fake.call_count
    probe(fake, ttl=0)
    assert fake.call_count > first


def test_require_capability_passes_when_proven(fake):
    script_healthy(fake)
    result = probe(fake)

    require_capability("htable.delete", result)
    require_capability("htable.delete", rpc=fake)
    assert capabilities.cached() is not None
    require_capability("htable.delete")  # module cache


def test_require_capability_raises_when_probe_failed(fake):
    script_healthy(fake)
    fake.fault("htable.delete")
    clear_cache()
    result = probe(fake)

    with pytest.raises(KamailioUnavailable) as excinfo:
        require_capability("htable.delete", result)
    assert "htable.delete" in str(excinfo.value)
    assert "refusing" in str(excinfo.value)


def test_require_capability_raises_when_unknown():
    clear_cache()
    with pytest.raises(KamailioUnavailable):
        require_capability("htable.delete")


def test_require_capability_raises_for_unreachable_rpc(tmp_path):
    rpc = UnixDatagramRPC(str(tmp_path / "missing.sock"), timeout_ms=20)
    result = probe(rpc)

    with pytest.raises(KamailioUnavailable):
        require_capability("htable.delete", result)


def test_probe_treats_htable_delete_404_as_capability_present(fake):
    script_healthy(fake)
    # Kamailio returns 404 when the probe key does not exist; the handler and
    # the regcache table are present, so the capability is usable.
    fake.fault("htable.delete", code=404, message="Key not found in htable.")

    result = probe(fake)

    assert result["htable.delete"] is True
    assert result["ok"] is True
    assert "htable.delete" not in missing_capabilities(result)


def test_probe_never_invokes_reload_rpcs(fake):
    script_healthy(fake)
    probe(fake)
    for method in RELOAD_METHODS:
        assert fake.calls_for(method) == []


def test_probe_can_skip_the_destructive_delete_check(fake):
    script_healthy(fake)
    result = probe(fake, allow_delete=False)
    assert fake.calls_for("htable.delete") == []
    assert result["htable.delete"] is False
