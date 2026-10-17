import json
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from judge.client import JudgeClient, JudgeDeadline, JudgeError, JudgeMalformed, JudgeRateLimited
from judge.server import make_handler

SECRET = "s" * 40


def serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def stop(server):
    server.shutdown()
    server.server_close()


def scripted(responses):
    """A fake judge that replies with scripted (status, body) pairs, in order."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            calls.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            status, body = responses[min(len(calls) - 1, len(responses) - 1)]
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return H, calls


OK = (200, {"model": "jev-1.13.0", "answers": {"q": {"type": "noul", "noul": 0.7}}, "usage": {}, "request_id": "r1"})


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class JudgeClientTest(unittest.TestCase):
    def client(self, url, clock=None):
        clock = clock or FakeClock()
        return JudgeClient(url, SECRET, clock=clock, sleep=clock.sleep), clock

    def test_ok_and_auth_header(self):
        H, calls = scripted([OK])
        server, url = serve(H)
        try:
            c, clock = self.client(url)
            r = c.ask({"a": 1}, {"q": {"type": "noul", "instructions": "?"}}, deadline=10)
            self.assertEqual(r.model, "jev-1.13.0")
            self.assertEqual(calls[0][0], f"Bearer {SECRET}")
        finally:
            stop(server)

    def test_malformed_never_retried(self):
        H, calls = scripted([(400, {"error": "malformed_question", "detail": "bad"}), OK])
        server, url = serve(H)
        try:
            c, _ = self.client(url)
            with self.assertRaises(JudgeMalformed):
                c.ask("s", {"q": {}}, deadline=10)
            self.assertEqual(len(calls), 1)
        finally:
            stop(server)

    def test_rate_limit_backs_off_then_succeeds(self):
        H, calls = scripted([(429, {"error": "rate_limited", "retry_after_ms": 1500}), OK])
        server, url = serve(H)
        try:
            c, clock = self.client(url)
            self.assertEqual(c.ask("s", {"q": {}}, deadline=10).model, "jev-1.13.0")
            self.assertEqual(len(calls), 2)
            self.assertGreaterEqual(clock.t, 1.5)
        finally:
            stop(server)

    def test_rate_limit_past_deadline_gives_up(self):
        H, calls = scripted([(429, {"error": "rate_limited", "retry_after_ms": 60_000})])
        server, url = serve(H)
        try:
            c, _ = self.client(url)
            with self.assertRaises(JudgeRateLimited) as cm:
                c.ask("s", {"q": {}}, deadline=10)
            self.assertEqual(cm.exception.retry_after_s, 60)
            self.assertEqual(len(calls), 1)
        finally:
            stop(server)

    def test_deadline_already_passed(self):
        clock = FakeClock()
        clock.t = 11
        c = JudgeClient("http://127.0.0.1:9", SECRET, clock=clock, sleep=clock.sleep)
        with self.assertRaises(JudgeDeadline):
            c.ask("s", {"q": {}}, deadline=10)

    def test_unauthorized_is_not_retried(self):
        H, calls = scripted([(401, {"error": "unauthorized"})])
        server, url = serve(H)
        try:
            c, _ = self.client(url)
            with self.assertRaises(JudgeError):
                c.ask("s", {"q": {}}, deadline=10)
            self.assertEqual(len(calls), 1)
        finally:
            stop(server)


def fake_sdk():
    """Stand-in for typesafe_sdk's error classes and RetryPolicy (names checked against the real SDK below)."""
    m = types.SimpleNamespace()

    class TypeSafeError(Exception):
        pass

    class TypeSafeAPIError(TypeSafeError):
        def __init__(self, status=500):
            super().__init__(status)
            self.status = status

    class TypeSafeAPIConnectionError(TypeSafeError):
        pass

    m.TypeSafeError = TypeSafeError
    m.TypeSafeAPIError = TypeSafeAPIError
    m.TypeSafeAPIConnectionError = TypeSafeAPIConnectionError
    m.TypeSafeAPITimeoutError = type("TypeSafeAPITimeoutError", (TypeSafeAPIConnectionError,), {})
    m.TypeSafeBadRequestError = type("TypeSafeBadRequestError", (TypeSafeAPIError,), {})
    m.TypeSafeUnprocessableEntityError = type("TypeSafeUnprocessableEntityError", (TypeSafeAPIError,), {})
    m.TypeSafeAuthenticationError = type("TypeSafeAuthenticationError", (TypeSafeAPIError,), {})

    class RateLimit(TypeSafeAPIError):
        retry_after_ms = 2000.0

    m.TypeSafeRateLimitError = RateLimit
    m.RetryPolicy = lambda **kw: kw
    return m


class FakeSDKClient:
    def __init__(self, raise_=None):
        self.raise_, self.calls = raise_, []

    def system_one(self, **kw):
        self.calls.append(kw)
        if self.raise_:
            raise self.raise_
        raw = types.SimpleNamespace(
            json=lambda: {"model": "jev-1.13.0", "answers": {"q": {"type": "noul", "noul": 0.4}}, "usage": {"input_tokens": 9}},
            headers={"x-typesafe-request-id": "req-9"},
        )
        return types.SimpleNamespace(raw_http_response=raw)


def post(url, body, secret=SECRET):
    req = urllib.request.Request(url + "/v1/judge", data=json.dumps(body).encode(), method="POST", headers={"Authorization": f"Bearer {secret}", "X-Timeout-S": "2.5"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class JudgeServerTest(unittest.TestCase):
    BODY = {"state": {"mid": 1}, "questions": {"q": {"type": "noul", "instructions": "?"}}}

    def run_server(self, sdk_client):
        sdk = fake_sdk()
        server, url = serve(make_handler(sdk_client, "jev-1.13.0", SECRET, sdk))
        self.addCleanup(stop, server)
        return url, sdk

    def test_passes_answers_raw_with_pinned_model(self):
        client = FakeSDKClient()
        url, _ = self.run_server(client)
        status, body = post(url, self.BODY)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"model": "jev-1.13.0", "answers": {"q": {"type": "noul", "noul": 0.4}}, "usage": {"input_tokens": 9}, "request_id": "req-9"})
        self.assertEqual(client.calls[0]["model"], "jev-1.13.0")
        self.assertEqual(client.calls[0]["timeout"], 2.5)
        self.assertEqual(client.calls[0]["retry"], {"max_retries": 0})

    def test_rejects_bad_secret(self):
        client = FakeSDKClient()
        url, _ = self.run_server(client)
        self.assertEqual(post(url, self.BODY, secret="wrong")[0], 401)
        self.assertEqual(client.calls, [])

    def test_malformed_question_rejected_before_jev(self):
        client = FakeSDKClient()
        url, _ = self.run_server(client)
        status, body = post(url, {"state": "x", "questions": {"q": {"type": "essay"}}})
        self.assertEqual((status, body["error"]), (400, "malformed_question"))
        self.assertEqual(client.calls, [])

    def test_maps_sdk_errors(self):
        sdk = fake_sdk()
        cases = [(sdk.TypeSafeRateLimitError(429), 429, "rate_limited"), (sdk.TypeSafeBadRequestError(400), 400, "malformed_question"),
                 (sdk.TypeSafeUnprocessableEntityError(422), 400, "malformed_question"), (sdk.TypeSafeAuthenticationError(401), 502, "upstream_auth_failed"),
                 (sdk.TypeSafeAPITimeoutError(), 504, "upstream_timeout")]
        for exc, status, code in cases:
            server, url = serve(make_handler(FakeSDKClient(exc), "jev-1.13.0", SECRET, sdk))
            try:
                got_status, body = post(url, self.BODY)
                self.assertEqual((got_status, body["error"]), (status, code), type(exc).__name__)
            finally:
                stop(server)


class RealSDKNamesTest(unittest.TestCase):
    def test_fake_matches_real_sdk(self):
        try:
            import typesafe_sdk
        except ImportError:
            self.skipTest("typesafe-sdk not installed")
        for name in vars(fake_sdk()):
            self.assertTrue(hasattr(typesafe_sdk, name), name)


if __name__ == "__main__":
    unittest.main()
