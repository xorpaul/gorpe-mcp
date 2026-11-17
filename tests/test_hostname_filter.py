import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

MESSAGE = "These hosts do not have gorpe configured and can not be queried"


def setup_function(_):
    server._hostname_filters = server._parse_hostname_filters(
        [{"pattern": "*foo*.domain.tld", "message": MESSAGE}]
    )


def teardown_function(_):
    server._hostname_filters = []


def _no_network(*a, **kw):
    raise AssertionError("network call must not happen")


def test_matching_host_is_filtered():
    assert server._check_hostname_filter("foobar.domain.tld") == {
        "exit_code": 3,
        "output": "matches hostname filter",
        "info": MESSAGE,
    }


def test_non_matching_host_passes():
    assert server._check_hostname_filter("bar.domain.tld") is None


def test_gorpe_run_returns_immediately_without_network(monkeypatch):
    monkeypatch.setattr(server, "_gorpe_request", _no_network)
    out = asyncio.run(server.gorpe_run("foobar.domain.tld", "check_w"))
    assert out["output"] == "matches hostname filter"
    assert out["info"] == MESSAGE


def test_gorpe_run_via_proxy_filters_target(monkeypatch):
    monkeypatch.setattr(server, "_gorpe_request", _no_network)
    out = asyncio.run(server.gorpe_run_via_proxy("proxy.domain.tld", "foobar.domain.tld", "check_w"))
    assert out["output"] == "matches hostname filter"
