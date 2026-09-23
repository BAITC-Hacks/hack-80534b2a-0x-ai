"""ASGI boundary checks without network requests or application databases."""
import unittest

from request_guard import RequestBodyLimitMiddleware


class RequestGuardTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, chunks, *, path='/api/complete', headers=None,
                     limit=8, import_limit=16, disconnect=False):
        self.called = False
        self.observed = None
        self.sent = []
        messages = [{'type': 'http.request', 'body': chunk,
                     'more_body': i < len(chunks) - 1 or disconnect}
                    for i, chunk in enumerate(chunks)]
        if disconnect:
            messages.append({'type': 'http.disconnect'})

        async def receive():
            return messages.pop(0) if messages else {'type': 'http.disconnect'}

        async def send(message):
            self.sent.append(message)

        async def downstream(scope, receive, send):
            self.called = True
            self.observed = await receive()
            self.next_message = await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        guard = RequestBodyLimitMiddleware(downstream, default_limit=limit,
                                           import_limit=import_limit)
        await guard({'type': 'http', 'path': path, 'headers': headers or []}, receive, send)

    async def test_exact_limit_is_delivered_once(self):
        await self.invoke([b'1234', b'5678'])
        self.assertTrue(self.called)
        self.assertEqual(self.observed, {'type': 'http.request', 'body': b'12345678', 'more_body': False})
        self.assertEqual(self.next_message['type'], 'http.disconnect')

    async def test_chunked_overflow_never_dispatches(self):
        await self.invoke([b'1234', b'56789'])
        self.assertFalse(self.called)
        self.assertEqual(self.sent[0]['status'], 413)

    async def test_oversized_declared_body_rejected_before_read(self):
        await self.invoke([], headers=[(b'content-length', b'9')])
        self.assertFalse(self.called)
        self.assertEqual(self.sent[0]['status'], 413)

    async def test_false_small_content_length_does_not_bypass_limit(self):
        await self.invoke([b'123456789'], headers=[(b'content-length', b'1')])
        self.assertFalse(self.called)
        self.assertEqual(self.sent[0]['status'], 413)

    async def test_import_has_separate_limit(self):
        for path in ('/api/import', '/api/import/'):
            await self.invoke([b'1' * 16], path=path)
            self.assertTrue(self.called)
            await self.invoke([b'1' * 17], path=path)
            self.assertFalse(self.called)
            self.assertEqual(self.sent[0]['status'], 413)

    async def test_disconnect_before_complete_body_never_dispatches(self):
        await self.invoke([b'12'], disconnect=True)
        self.assertFalse(self.called)
        self.assertEqual(self.sent, [])

    async def test_invalid_or_conflicting_length(self):
        for headers in ([(b'content-length', b'-1')],
                        [(b'content-length', b'bad')],
                        [(b'content-length', b'1'), (b'content-length', b'2')]):
            await self.invoke([], headers=headers)
            self.assertFalse(self.called)
            self.assertEqual(self.sent[0]['status'], 400)

    async def test_downstream_error_is_not_rewritten_as_413(self):
        messages = []

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            messages.append(message)

        async def downstream(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            raise RuntimeError('application failure')

        with self.assertRaisesRegex(RuntimeError, 'application failure'):
            await RequestBodyLimitMiddleware(downstream)(
                {'type': 'http', 'path': '/', 'headers': []}, receive, send)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['status'], 200)

    async def test_non_http_scope_passes_through(self):
        observed = []

        async def downstream(scope, receive, send):
            observed.append(scope['type'])

        await RequestBodyLimitMiddleware(downstream)({'type': 'lifespan'}, None, None)
        self.assertEqual(observed, ['lifespan'])


if __name__ == '__main__':
    unittest.main()
