import asyncio
import json

import pytest
from app.middleware.request_size import (
    RequestBodyLimitMiddleware,
    _RequestBodyTooLarge,
)


def run_asgi(app, *, scope, messages):
    sent = []
    pending = list(messages)

    async def receive():
        if pending:
            return pending.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def invoke():
        await app(scope, receive, send)

    asyncio.run(invoke())
    return sent


def http_scope(headers=None, method="POST"):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/probe",
        "raw_path": b"/probe",
        "query_string": b"",
        "headers": headers or [],
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
    }


class TestRequestBodyLimitMiddleware:
    def test_rejects_non_positive_limit(self):
        with pytest.raises(ValueError, match="max_body_bytes must be positive"):
            RequestBodyLimitMiddleware(lambda *_: None, max_body_bytes=0)

        with pytest.raises(ValueError, match="max_body_bytes must be positive"):
            RequestBodyLimitMiddleware(lambda *_: None, max_body_bytes=-1)

    @pytest.mark.parametrize("value", [b"abc", b"-1"])
    def test_rejects_invalid_or_negative_content_length(self, value):
        async def downstream(scope, receive, send):
            raise AssertionError("Downstream must not run for invalid Content-Length")

        sent = run_asgi(
            RequestBodyLimitMiddleware(downstream, max_body_bytes=4),
            scope=http_scope([(b"content-length", value)]),
            messages=[],
        )
        assert sent[0]["status"] == 400
        body = json.loads(sent[1]["body"])
        assert body["error"]["code"] == "invalid_content_length"

    def test_rejects_declared_body_larger_than_limit(self):
        async def downstream(scope, receive, send):
            raise AssertionError("Downstream must not run for oversized body")

        sent = run_asgi(
            RequestBodyLimitMiddleware(downstream, max_body_bytes=4),
            scope=http_scope([(b"content-length", b"5")]),
            messages=[],
        )
        assert sent[0]["status"] == 413
        body = json.loads(sent[1]["body"])
        assert body["error"]["code"] == "request_body_too_large"

    def test_accepts_body_at_exact_limit(self):
        async def downstream(scope, receive, send):
            message = await receive()
            assert message["body"] == b"1234"
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        sent = run_asgi(
            RequestBodyLimitMiddleware(downstream, max_body_bytes=4),
            scope=http_scope([(b"content-length", b"4")]),
            messages=[{"type": "http.request", "body": b"1234", "more_body": False}],
        )
        assert sent[0]["status"] == 200

    def test_rejects_streamed_body_that_exceeds_limit_without_content_length(self):
        async def downstream(scope, receive, send):
            await receive()
            await receive()

        with pytest.raises(_RequestBodyTooLarge):
            run_asgi(
                RequestBodyLimitMiddleware(downstream, max_body_bytes=4),
                scope=http_scope(),
                messages=[
                    {"type": "http.request", "body": b"123", "more_body": True},
                    {"type": "http.request", "body": b"45", "more_body": False},
                ],
            )

    def test_non_http_scope_bypasses_http_body_limit_logic(self):
        called = []

        async def downstream(scope, receive, send):
            called.append(scope["type"])

        sent = run_asgi(
            RequestBodyLimitMiddleware(downstream, max_body_bytes=1),
            scope={"type": "lifespan"},
            messages=[],
        )
        assert sent == []
        assert called == ["lifespan"]
