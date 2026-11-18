"""Strict JSON input and expected Web error status codes."""

import os
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ASTRBOT_ROOT", str(ROOT / ".verification" / "astrbot"))

from astrbot.api.web import PluginRequest, bind_request_context  # noqa: E402
from starlette.requests import Request  # noqa: E402

from core.maintenance import MaintenanceUnavailable  # noqa: E402
from core.service import UserFacingError  # noqa: E402
from core.webui import WebUIMixin, web_endpoint  # noqa: E402


def raw_request(raw=b"{}", *, content_length=None):
    headers = [(b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    receive = AsyncMock(return_value={"type": "http.request", "body": raw, "more_body": False})
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/test",
            "query_string": b"",
            "headers": headers,
        },
        receive,
    )
    return PluginRequest(
        request, plugin_name="astrbot_plugin_youtube_music", username="admin"
    ), receive


class WebHarness(WebUIMixin):
    _running = True
    _terminating = False
    failure = None

    @asynccontextmanager
    async def operation(self):
        yield

    @web_endpoint
    async def endpoint(self, username):
        if self.failure:
            raise self.failure
        return await self._body({"action", "confirmed"})


class WebInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.web = WebHarness()

    async def test_declared_oversize_and_invalid_length_are_rejected_before_read(self):
        for length in (8193, -1, "unknown", "9" * 20, "1, 1"):
            with self.subTest(length=length):
                request, receive = raw_request(content_length=length)
                with bind_request_context(request):
                    response = await self.web.endpoint()
                self.assertEqual(response.status_code, 400)
                receive.assert_not_awaited()

    async def test_missing_or_understated_length_does_not_bypass_actual_size_limit(self):
        for length in (None, 2):
            request, receive = raw_request(b" " * 8193, content_length=length)
            with bind_request_context(request):
                response = await self.web.endpoint()
            self.assertEqual(response.status_code, 400)
            receive.assert_awaited_once()

    async def test_duplicate_nonfinite_nested_and_nonobject_json_are_rejected(self):
        for raw in (
            b'{"confirmed":false,"confirmed":true}',
            b'{"action":NaN}',
            b'{"action":Infinity}',
            b'{"action":-Infinity}',
            b'{"action":1e309}',
            b'{"action":-1e309}',
            b'{"action":' + b"[" * 3000 + b"0" + b"]" * 3000 + b"}",
            b"[]",
            b"null",
            b"\xff",
        ):
            with self.subTest(raw=raw[:80]):
                request, _ = raw_request(raw)
                with bind_request_context(request):
                    response = await self.web.endpoint()
                self.assertEqual(response.status_code, 400)

    async def test_valid_json_and_unknown_fields_keep_the_existing_contract(self):
        request, _ = raw_request(b'{"action":"update","confirmed":true}', content_length=36)
        with bind_request_context(request):
            self.assertEqual(await self.web.endpoint(), {"action": "update", "confirmed": True})
        request, _ = raw_request(b'{"unexpected":true}')
        with bind_request_context(request):
            response = await self.web.endpoint()
        self.assertEqual(response.status_code, 400)

    async def test_maintenance_stays_503_and_user_errors_stay_400(self):
        for failure, expected in (
            (MaintenanceUnavailable("Busy"), 503),
            (UserFacingError("Queue full"), 400),
            (ValueError("Invalid input"), 400),
            (RuntimeError("synthetic-secret"), 500),
        ):
            self.web.failure = failure
            request, _ = raw_request()
            with bind_request_context(request):
                response = await self.web.endpoint()
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("synthetic-secret", response.body.decode())

    async def test_argument_free_commands_validate_json_before_mutating(self):
        clear = Mock(return_value={"deleted": 0})
        self.web.audio_cache = SimpleNamespace(clear_unprotected=clear)
        self.web._dependency_update_task = None
        for handler in (self.web.web_cache_cleanup, self.web.web_dependency_cancel):
            for raw in (b'{"unexpected":true}', b"null", b'{"extra":1e309}', b" " * 8193):
                with self.subTest(handler=handler.__name__, raw=raw[:80]):
                    request, _ = raw_request(raw)
                    with bind_request_context(request):
                        response = await handler()
                    self.assertEqual(response.status_code, 400)
            clear.assert_not_called()
        for handler in (self.web.web_cache_cleanup, self.web.web_dependency_cancel):
            request, _ = raw_request()
            with bind_request_context(request):
                response = await handler()
            self.assertEqual(response.status_code, 200)
        clear.assert_called_once()
