"""Exercise AstrBot 4.29.4 plugin-scope authentication with synthetic credentials."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault(
    "ASTRBOT_ROOT", str(Path(tempfile.gettempdir()) / "youtube-music-runtime-tests")
)

import httpx  # noqa: E402
import jwt  # noqa: E402
from astrbot.dashboard.api.auth import require_scope  # noqa: E402
from astrbot.dashboard.api.plugins import router  # noqa: E402
from astrbot.dashboard.responses import ApiError  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

SECRET = "synthetic-test-only-jwt-secret-32-bytes"


class OfficialAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def request(self, headers=(), query="", db=None):
        app = SimpleNamespace(state=SimpleNamespace(jwt_secret=SECRET, db=db))
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/plugins/extensions/test",
                "query_string": query.encode(),
                "headers": list(headers),
                "app": app,
            }
        )

    async def test_absent_auth_and_spoofed_username_are_unauthorized(self):
        for request in (
            self.request(),
            self.request(headers=((b"x-username", b"admin"),), query="username=admin"),
        ):
            with self.assertRaises(ApiError) as caught:
                await require_scope(request, "plugin")
            self.assertEqual(caught.exception.status_code, 401)

    async def test_official_jwt_principal_is_authorized_without_plugin_whitelist(self):
        token = jwt.encode(
            {"username": "operator", "exp": time.time() + 60}, SECRET, algorithm="HS256"
        )
        auth = await require_scope(
            self.request(headers=((b"authorization", f"Bearer {token}".encode()),)),
            "plugin",
        )
        self.assertEqual(auth.username, "operator")
        self.assertEqual(auth.scopes, ["*"])

    async def test_expired_jwt_is_unauthorized(self):
        token = jwt.encode({"username": "operator", "exp": 1}, SECRET, algorithm="HS256")
        with self.assertRaises(ApiError) as caught:
            await require_scope(
                self.request(headers=((b"cookie", f"astrbot_jwt={token}".encode()),)), "plugin"
            )
        self.assertEqual(caught.exception.status_code, 401)

    async def test_api_key_requires_plugin_scope_and_uses_host_principal(self):
        for scopes, allowed in ((["plugin"], True), (["system"], False)):
            db = SimpleNamespace(
                get_active_api_key_by_hash=AsyncMock(
                    return_value=SimpleNamespace(scopes=scopes, key_id="synthetic")
                ),
                touch_api_key=AsyncMock(),
            )
            request = self.request(headers=((b"x-api-key", b"synthetic-test-key"),), db=db)
            if allowed:
                auth = await require_scope(request, "plugin")
                self.assertEqual(auth.api_key_id, "synthetic")
                self.assertEqual(auth.via, "api_key")
                db.touch_api_key.assert_awaited_once()
            else:
                with self.assertRaises(ApiError) as caught:
                    await require_scope(request, "plugin")
                self.assertEqual(caught.exception.status_code, 403)
                db.touch_api_key.assert_not_awaited()

    def test_all_five_extension_methods_require_official_plugin_scope(self):
        routes = [
            route
            for route in router.routes
            if route.path == "/plugins/extensions/{plugin_path:path}"
        ]
        self.assertEqual(
            {method for route in routes for method in route.methods},
            {"GET", "POST", "PUT", "PATCH", "DELETE"},
        )
        for route in routes:
            self.assertTrue(
                any(
                    dep.call.scope == "plugin"
                    for dep in route.dependant.dependencies
                    if hasattr(dep.call, "scope")
                )
            )

    async def test_real_extension_http_route_authenticates_before_dispatch(self):
        app = FastAPI()
        app.state.jwt_secret = SECRET
        app.include_router(router, prefix="/api/v1")
        app.state.db = SimpleNamespace(
            get_active_api_key_by_hash=AsyncMock(
                return_value=SimpleNamespace(scopes=["im"], key_id="synthetic")
            ),
            touch_api_key=AsyncMock(),
        )

        @app.exception_handler(ApiError)
        async def auth_error(_request, exc):
            return JSONResponse({"error": "auth"}, status_code=exc.status_code)

        token = jwt.encode(
            {"username": "operator", "exp": time.time() + 60}, SECRET, algorithm="HS256"
        )
        path = "/api/v1/plugins/extensions/astrbot_plugin_youtube_music/status"
        with patch(
            "astrbot.dashboard.api.plugins._call_plugin_extension",
            new=AsyncMock(return_value={"ok": True}),
        ) as dispatch:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for headers, code in (
                    ({"x-username": "operator"}, 401),
                    ({"x-api-key": "synthetic-test-key"}, 403),
                    ({"authorization": f"Bearer {token}"}, 200),
                ):
                    response = await client.get(path + "?username=spoofed", headers=headers)
                    self.assertEqual(response.status_code, code)
            dispatch.assert_awaited_once()
            self.assertEqual(dispatch.call_args.args[-1], "operator")
