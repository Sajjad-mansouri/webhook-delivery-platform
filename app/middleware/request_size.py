from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Reject HTTP request bodies larger than the configured limit."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = 1_048_576,
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive.")

        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = None

        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    content_length = int(value)
                except ValueError:
                    await self._reject(send, 400, "invalid_content_length")
                    return
                break

        if content_length is not None:
            if content_length < 0:
                await self._reject(send, 400, "invalid_content_length")
                return

            if content_length > self.max_body_bytes:
                await self._reject(send, 413, "request_body_too_large")
                return

        bytes_received = 0

        async def limited_receive() -> Message:
            nonlocal bytes_received

            message = await receive()

            if message["type"] == "http.request":
                bytes_received += len(message.get("body", b""))

                if bytes_received > self.max_body_bytes:
                    raise _RequestBodyTooLarge

            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            # The application may already have begun its response. In that
            # case a replacement 413 response cannot safely be sent.
            # Configure an outer server/proxy body limit as well.
            raise

    @staticmethod
    async def _reject(
        send: Send,
        status_code: int,
        code: str,
    ) -> None:
        import json

        body = json.dumps(
            {"error": {"code": code, "message": "Request rejected."}}
        ).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )


class _RequestBodyTooLarge(Exception):
    pass
