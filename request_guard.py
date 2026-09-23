"""Bound HTTP request bodies before FastAPI parses JSON or multipart uploads."""
from io import BytesIO


class RequestBodyLimitMiddleware:
    """Buffer only a bounded body, including requests without Content-Length.

    Register outside routing/form parsing. No downstream exceptions are caught:
    application errors must keep their normal error handling and response.
    """

    def __init__(self, app, *, default_limit=64 * 1024, import_limit=10_100_000):
        self.app = app
        self.default_limit = default_limit
        self.import_limit = import_limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        limit = (self.import_limit if scope.get('path', '').rstrip('/') == '/api/import'
                 else self.default_limit)
        declared = []
        for name, value in scope.get('headers', []):
            if name.lower() == b'content-length':
                # Reject ambiguous lengths rather than trusting the first header.
                if not value or not value.isdigit() or len(value) > 20:
                    return await self.reject(send, 400, b'{"detail":"Invalid Content-Length"}')
                declared.append(int(value))
        if declared and len(set(declared)) != 1:
            return await self.reject(send, 400, b'{"detail":"Conflicting Content-Length"}')
        if declared and declared[0] > limit:
            return await self.reject(send, 413, b'{"detail":"Request body too large"}')

        # Buffer before dispatch: form parsers must never see an oversized body.
        # BytesIO avoids retaining an unbounded list of tiny incoming chunks.
        with BytesIO() as buffer:
            size = 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > limit:
                    return await self.reject(send, 413, b'{"detail":"Request body too large"}')
                buffer.write(chunk)
                if not message.get('more_body', False):
                    break
            body = buffer.getvalue()

        delivered = False

        async def bounded_receive():
            nonlocal delivered, body
            if not delivered:
                delivered = True
                message = {'type': 'http.request', 'body': body, 'more_body': False}
                body = b''
                return message
            # Preserve actual disconnect semantics for downstream streaming code.
            return await receive()

        await self.app(scope, bounded_receive, send)

    @staticmethod
    async def reject(send, status, body):
        await send({'type': 'http.response.start', 'status': status, 'headers': [
            (b'content-type', b'application/json'),
            (b'content-length', str(len(body)).encode('ascii')),
            (b'cache-control', b'no-store'),
            (b'x-content-type-options', b'nosniff'),
        ]})
        await send({'type': 'http.response.body', 'body': body})
