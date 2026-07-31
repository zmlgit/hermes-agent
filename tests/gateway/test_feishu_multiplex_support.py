"""Regression tests for feishu multiplex_profiles support.

Background
----------
These tests guard the three commits that together let a single multiplexing
gateway serve multiple Feishu apps (one per profile) — enabling multi-bot
group-discussion scenarios that ``profile_routes`` cannot express (it routes
messages from one bot to different personas; users see a single bot talking
to itself, defeating the "panel of experts" UX).

The upstream code already implies support for feishu in secondary profiles
via ``gateway/config.py::PORT_BINDING_CONDITIONAL_MODES = {"feishu":
"webhook"}`` — feishu in websocket mode doesn't bind a port, so it should be
safe under multiplexing. But the implementation has three gaps that this
PR closes:

1. ``_apply_yaml_config`` — translate ``feishu:`` YAML block into the
   ``FEISHU_*`` env vars the plugin loader requires, so a secondary profile
   can declare its own feishu app via per-profile YAML.
2. ``_ThreadLocalLoopProxy`` — isolate the ``lark_oapi.ws.Client`` event loop
   per-adapter, so multiple WS clients in one process don't trip
   ``Task ... attached to a different loop``.
3. ``per-instance hermes_loop`` — bind SDK client methods to the adapter's
   own loop rather than the module-global loop the SDK assumes.

If any of the three regresses, the corresponding test below fails loudly
with a pointer to the mechanism that broke.
"""
from __future__ import annotations

import os
import unittest
from typing import Dict, Any
from unittest.mock import patch


class FeishuMultiplexYamlConfigTest(unittest.TestCase):
    """Verifies ``_apply_yaml_config`` translates per-profile YAML into the
    form the plugin loader / FeishuAdapterSettings can consume."""

    def test_apply_yaml_config_seeds_app_credentials_into_extra(self):
        from plugins.platforms.feishu.adapter import _apply_yaml_config

        feishu_cfg = {
            "app_id": "cli_test_app_id",
            "app_secret": "secret_value",
            "encrypt_key": "enc",
            "verification_token": "tok",
            "domain": "feishu",
            "connection_mode": "websocket",
            "extra": {"default_group_policy": "open"},
        }
        # Clear env to assert the function does NOT pollute it for app creds
        # (allow_bots is the one exception, see below).
        with patch.dict(os.environ, {}, clear=False):
            for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_ALLOW_BOTS"):
                os.environ.pop(k, None)
            seeded = _apply_yaml_config({}, feishu_cfg)

        # All six credential/behavior keys flow into extra, so a secondary
        # profile's app_id is visible to the plugin loader via config.extra
        # even though FEISHU_APP_ID env is never set globally.
        self.assertIsNotNone(seeded, "_apply_yaml_config must return a non-None seeded dict")
        self.assertEqual(seeded["app_id"], "cli_test_app_id")
        self.assertEqual(seeded["app_secret"], "secret_value")
        self.assertEqual(seeded["encrypt_key"], "enc")
        self.assertEqual(seeded["verification_token"], "tok")
        self.assertEqual(seeded["domain"], "feishu")
        self.assertEqual(seeded["connection_mode"], "websocket")
        # Existing extra keys are preserved.
        self.assertEqual(seeded["default_group_policy"], "open")

    def test_apply_yaml_config_does_not_leak_app_secret_into_os_environ(self):
        """A secondary profile's app_secret must NOT be written to
        os.environ — that would leak it to every other profile's turn under
        the multiplexer. Only allow_bots is allowed to seed env (for the
        legacy auth-bypass path)."""
        from plugins.platforms.feishu.adapter import _apply_yaml_config

        with patch.dict(os.environ, {}, clear=False):
            for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET"):
                os.environ.pop(k, None)
            _apply_yaml_config({}, {
                "app_id": "cli_x",
                "app_secret": "should_not_leak",
            })
            self.assertNotIn("FEISHU_APP_ID", os.environ)
            self.assertNotIn("FEISHU_APP_SECRET", os.environ)

    def test_apply_yaml_config_returns_none_for_empty_feishu_block(self):
        """No feishu config → no seed → plugin loader uses env as before."""
        from plugins.platforms.feishu.adapter import _apply_yaml_config

        self.assertIsNone(_apply_yaml_config({}, {}))
        self.assertIsNone(_apply_yaml_config({}, {"extra": {}}))


class FeishuMultiplexLoopIsolationTest(unittest.TestCase):
    """Verifies the per-adapter WS event loop isolation that lets N feishu
    WS clients coexist in one gateway process.

    Without this, the second profile's WS connect trips:
        ``RuntimeError: Task ... got Future ... attached to a different loop``
    """

    def test_thread_local_loop_proxy_symbol_is_present(self):
        """``_ThreadLocalLoopProxy`` is the class that scopes the SDK's
        module-global loop to the calling thread. Importing it must not fail.
        If a future refactor renames or removes it, this test fires first
        rather than letting production hit the cross-loop Task error."""
        from plugins.platforms.feishu import adapter

        # The class may live at module scope or as a FeishuAdapter attribute;
        # accept either.
        self.assertTrue(
            hasattr(adapter, "_ThreadLocalLoopProxy")
            or any("_ThreadLocalLoopProxy" in type(getattr(adapter, n, None)).__name__
                   for n in dir(adapter) if not n.startswith("__")),
            "_ThreadLocalLoopProxy is missing — multi-profile feishu WS will "
            "hit 'Task attached to a different loop' (see PR description).",
        )

    def test_run_official_feishu_ws_client_isolates_hermes_loop_per_instance(self):
        """Two SDK clients run through ``_run_official_feishu_ws_client``
        each get their own ``_hermes_loop`` and bound patched methods.

        This is the behavioral guard for the per-instance loop binding: we
        call the real function against a fake SDK and observe the state it
        mutates on each client/adapter, rather than reading the adapter
        source. If a future refactor stops setting ``self._hermes_loop``
        or stops binding the patched methods per-instance, the second
        feishu profile's SDK calls will run on the first profile's loop
        ('Task got Future attached to a different loop')."""
        import asyncio
        import sys
        from types import ModuleType, MethodType, SimpleNamespace
        from unittest.mock import AsyncMock

        fake_client_module = ModuleType("lark_oapi.ws.client")
        fake_client_module.loop = None  # replaced in-place by _install_thread_local_ws_loop_proxy
        fake_client_module.logger = SimpleNamespace(
            info=lambda *_a, **_k: None,
            error=lambda *_a, **_k: None,
            warning=lambda *_a, **_k: None,
        )
        fake_client_module._parse_ws_conn_exception = lambda exc: (_ for _ in ()).throw(exc)

        async def _fake_select():
            return None

        fake_client_module._select = _fake_select
        fake_client_module.websockets = SimpleNamespace(
            connect=AsyncMock(return_value=None),
            InvalidStatusCode=RuntimeError,
        )

        fake_exception_module = ModuleType("lark_oapi.ws.exception")
        class _CExc(Exception):
            pass

        class _CCExc(Exception):
            pass

        fake_exception_module.ClientException = _CExc
        fake_exception_module.ConnectionClosedException = _CCExc

        fake_ws_module = ModuleType("lark_oapi.ws")
        fake_ws_module.client = fake_client_module
        fake_root_module = ModuleType("lark_oapi")
        fake_root_module.ws = fake_ws_module

        class _FakeWSClient:
            def __init__(self):
                self._lock = asyncio.Lock()
                self._conn = None
                self._auto_reconnect = True

            def _get_conn_url(self):
                return "wss://example.test/ws?device_id=d1&service_id=s1"

            async def _handle_message(self, _msg):
                return None

            async def _disconnect(self):
                self._conn = None

            async def _reconnect(self):
                return None

            async def _ping_loop(self):
                await asyncio.sleep(3600)

            def _fmt_log(self, template, *args):
                return template

            def start(self):  # replaced by _run_official_feishu_ws_client
                raise AssertionError("start was not patched")

        original_modules = sys.modules.copy()
        sys.modules["lark_oapi"] = fake_root_module
        sys.modules["lark_oapi.ws"] = fake_ws_module
        sys.modules["lark_oapi.ws.client"] = fake_client_module
        sys.modules["lark_oapi.ws.exception"] = fake_exception_module
        try:
            from plugins.platforms.feishu.adapter import _run_official_feishu_ws_client

            client1 = _FakeWSClient()
            adapter1 = SimpleNamespace()
            _run_official_feishu_ws_client(client1, adapter1)
            loop1 = client1._hermes_loop

            client2 = _FakeWSClient()
            adapter2 = SimpleNamespace()
            _run_official_feishu_ws_client(client2, adapter2)
            loop2 = client2._hermes_loop

            self.assertIsNotNone(loop1)
            self.assertIsNotNone(loop2)
            self.assertIsNot(
                loop1, loop2,
                "two feishu WS clients must not share _hermes_loop — "
                "the second profile's SDK calls would land on the first "
                "profile's loop ('Task attached to a different loop').",
            )
            for name in ("start", "_connect", "_receive_message_loop"):
                m1 = getattr(client1, name)
                m2 = getattr(client2, name)
                self.assertIsInstance(m1, MethodType, f"client1.{name} not bound")
                self.assertIsInstance(m2, MethodType, f"client2.{name} not bound")
                self.assertIs(m1.__self__, client1, f"client1.{name} bound to wrong instance")
                self.assertIs(m2.__self__, client2, f"client2.{name} bound to wrong instance")
        finally:
            sys.modules.clear()
            sys.modules.update(original_modules)

    def test_patched_receive_loop_skips_reconnect_when_auto_reconnect_disabled(self):
        """The patched receive loop must honor ``_auto_reconnect=False`` set
        by ``_disable_websocket_auto_reconnect`` during shutdown. Without
        this gate the loop's exception handler calls ``_reconnect`` and
        resuscitates a connection the gateway is actively tearing down —
        a reconnect race that leaves an orphaned WS thread alive after
        shutdown (the #69904 bug). We exercise the real patched loop
        against a fake conn whose recv() raises, with
        ``_auto_reconnect=False``, and assert ``_reconnect`` is NOT
        called."""
        import asyncio
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import AsyncMock

        class _ConnectionClosed(Exception):
            """Type name contains 'ConnectionClosed' as the patched loop
            checks via ``'ConnectionClosed' in type(e).__name__``."""

        class _FakeConn:
            async def recv(self):
                raise _ConnectionClosed("test close")

        fake_client_module = ModuleType("lark_oapi.ws.client")
        fake_client_module.loop = None
        fake_client_module.logger = SimpleNamespace(
            info=lambda *_a, **_k: None,
            error=lambda *_a, **_k: None,
            warning=lambda *_a, **_k: None,
        )
        fake_client_module._parse_ws_conn_exception = lambda exc: (_ for _ in ()).throw(exc)

        async def _fake_select():
            # Yield so the receive loop task (scheduled by _patched_connect)
            # runs on the live loop before _run_official_feishu_ws_client's
            # finally closes it. _auto_reconnect was flipped off before the
            # call to simulate shutdown's _disable_websocket_auto_reconnect.
            await asyncio.sleep(0.1)
            return None

        fake_client_module._select = _fake_select
        fake_client_module.websockets = SimpleNamespace(
            connect=AsyncMock(return_value=_FakeConn()),
            InvalidStatusCode=RuntimeError,
        )

        fake_exception_module = ModuleType("lark_oapi.ws.exception")
        class _CExc(Exception):
            pass

        class _CCExc(Exception):
            pass

        fake_exception_module.ClientException = _CExc
        fake_exception_module.ConnectionClosedException = _CCExc

        fake_ws_module = ModuleType("lark_oapi.ws")
        fake_ws_module.client = fake_client_module
        fake_root_module = ModuleType("lark_oapi")
        fake_root_module.ws = fake_ws_module

        class _FakeWSClient:
            def __init__(self):
                self._lock = asyncio.Lock()
                self._conn = None
                self._auto_reconnect = True
                self.reconnect_calls = 0
                self.disconnect_calls = 0

            def _get_conn_url(self):
                return "wss://example.test/ws?device_id=d1&service_id=s1"

            async def _handle_message(self, _msg):
                return None

            async def _disconnect(self):
                self.disconnect_calls += 1
                self._conn = None

            async def _reconnect(self):
                self.reconnect_calls += 1

            async def _ping_loop(self):
                await asyncio.sleep(3600)

            def _fmt_log(self, template, *args):
                return template

        original_modules = sys.modules.copy()
        sys.modules["lark_oapi"] = fake_root_module
        sys.modules["lark_oapi.ws"] = fake_ws_module
        sys.modules["lark_oapi.ws.client"] = fake_client_module
        sys.modules["lark_oapi.ws.exception"] = fake_exception_module
        try:
            from plugins.platforms.feishu.adapter import _run_official_feishu_ws_client

            client = _FakeWSClient()
            adapter = SimpleNamespace(
                _ws_reconnect_nonce=2,
                _ws_reconnect_interval=3,
                _ws_ping_interval=4,
                _ws_ping_timeout=5,
            )
            # Pre-flip _auto_reconnect off — simulates shutdown's
            # _disable_websocket_auto_reconnect having run before the
            # receive loop catches the closed-connection exception.
            # Production races the flag during teardown; this test pins the
            # steady-state at False and asserts the gate's behavior.
            client._auto_reconnect = False

            _run_official_feishu_ws_client(client, adapter)

            self.assertEqual(
                client.reconnect_calls, 0,
                "_reconnect must NOT be called when _auto_reconnect=False "
                "(shutdown path) — the #69904 reconnect race.",
            )
            self.assertGreaterEqual(
                client.disconnect_calls, 1,
                "_disconnect should still run so the dead conn is cleaned up.",
            )
        finally:
            sys.modules.clear()
            sys.modules.update(original_modules)


if __name__ == "__main__":
    unittest.main()
