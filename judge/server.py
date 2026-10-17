"""The judge: the only process that holds the TypeSafe key.

    TYPESAFE_API_KEY=... DESK_JUDGE_SECRET=... python -m judge.server

- Reads TYPESAFE_API_KEY from its own environment (the SDK does this); the key never
  leaves this process and is never logged.
- Accepts POST /v1/judge from the desk, authenticated with DESK_JUDGE_SECRET.
- Calls Jev once per request with the whole battery, on the pinned model only.
- Returns Jev's answers raw, plus the exact model version string and request id.
  It makes no trading decision and does not import any trading code.
- Does not retry. The desk owns backoff because only the desk knows its deadline.
"""

import hmac
import json
import logging
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desk.config import PINNED_JEV_MODEL, check_pinned_model

log = logging.getLogger("judge")

MAX_BODY_BYTES = 64_000
MIN_SECRET_LEN = 32
QUESTION_TYPES = {"noul", "choice", "score"}


class Malformed(ValueError):
    pass


def validate_request(body: object) -> tuple[object, dict]:
    if not isinstance(body, dict):
        raise Malformed("body must be a JSON object")
    state, questions = body.get("state"), body.get("questions")
    if not isinstance(state, (str, dict)) or not state:
        raise Malformed("state must be a non-empty string or object")
    if not isinstance(questions, dict) or not questions:
        raise Malformed("questions must be a non-empty object")
    for qid, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in QUESTION_TYPES:
            raise Malformed(f"question {qid!r} must be an object with type in {sorted(QUESTION_TYPES)}")
    return state, questions


def make_handler(sdk_client, model: str, desk_secret: str, errors):
    """Build the request handler. `errors` is the typesafe_sdk module (injected for tests)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "judge"

        def log_message(self, format, *args):  # default logger prints request lines; keep quiet
            pass

        def do_POST(self):
            started = time.monotonic()
            if self.path != "/v1/judge":
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {desk_secret}".encode()):
                return self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "malformed_question", "detail": "bad body size"})
            try:
                state, questions = validate_request(json.loads(self.rfile.read(length)))
            except (ValueError, Malformed) as e:
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "malformed_question", "detail": str(e)})
            try:
                timeout = max(0.1, float(self.headers.get("X-Timeout-S", "10")))
            except ValueError:
                timeout = 10.0

            try:
                resp = sdk_client.system_one(
                    state=state,
                    questions=questions,
                    model=model,
                    timeout=timeout,
                    retry=errors.RetryPolicy(max_retries=0),
                )
                raw = resp.raw_http_response.json()
                out = {
                    "model": raw["model"],
                    "answers": raw["answers"],
                    "usage": raw.get("usage"),
                    "request_id": resp.raw_http_response.headers.get("x-typesafe-request-id"),
                }
                status = HTTPStatus.OK
            except errors.TypeSafeRateLimitError as e:
                status, out = HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited", "retry_after_ms": e.retry_after_ms}
            except (errors.TypeSafeBadRequestError, errors.TypeSafeUnprocessableEntityError) as e:
                status, out = HTTPStatus.BAD_REQUEST, {"error": "malformed_question", "detail": str(e)}
            except errors.TypeSafeAuthenticationError:
                status, out = HTTPStatus.BAD_GATEWAY, {"error": "upstream_auth_failed"}
            except errors.TypeSafeAPITimeoutError:
                status, out = HTTPStatus.GATEWAY_TIMEOUT, {"error": "upstream_timeout"}
            except errors.TypeSafeAPIError as e:
                status, out = HTTPStatus.BAD_GATEWAY, {"error": f"upstream_{e.status}"}
            except errors.TypeSafeAPIConnectionError:
                status, out = HTTPStatus.BAD_GATEWAY, {"error": "upstream_unreachable"}
            except errors.TypeSafeError as e:  # raised before sending, e.g. an invalid question
                status, out = HTTPStatus.BAD_REQUEST, {"error": "malformed_question", "detail": str(e)}
            log.info("judge status=%d model=%s request_id=%s latency_ms=%.0f", status, out.get("model"), out.get("request_id"), (time.monotonic() - started) * 1000)
            self._send(status, out)

        def _send(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> None:
    import typesafe_sdk

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    model = os.environ.get("JEV_MODEL", PINNED_JEV_MODEL)
    check_pinned_model(model)
    desk_secret = os.environ.get("DESK_JUDGE_SECRET", "")
    if len(desk_secret) < MIN_SECRET_LEN:
        raise SystemExit(f"DESK_JUDGE_SECRET must be at least {MIN_SECRET_LEN} characters")
    if os.environ.get("TYPESAFE_LOG_LEVEL", "").lower() == "debug":
        raise SystemExit("refusing TYPESAFE_LOG_LEVEL=debug: the SDK logs full request and response bodies at debug")
    host = os.environ.get("JUDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("JUDGE_PORT", "8787"))

    client = typesafe_sdk.TypeSafeClient(model=model)  # reads TYPESAFE_API_KEY itself
    server = ThreadingHTTPServer((host, port), make_handler(client, model, desk_secret, typesafe_sdk))
    log.info("judge listening on %s:%d, model %s", host, port, model)
    try:
        server.serve_forever()
    finally:
        client.close()


if __name__ == "__main__":
    main()
