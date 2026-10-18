"""Desk-side client for the judge service.

The desk authenticates with DESK_JUDGE_SECRET and never holds the TypeSafe key.
Every call has a hard deadline (the start of the next block). Rate limits and transient
failures back off inside that deadline; a malformed-question error is never retried.
If the deadline passes, the caller holds.
"""

import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass


class JudgeError(Exception):
    check = "jev_unavailable"


class JudgeDeadline(JudgeError):
    check = "jev_deadline"


class JudgeRateLimited(JudgeError):
    check = "jev_rate_limited"

    def __init__(self, message: str, retry_after_s: float | None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class JudgeMalformed(JudgeError):
    check = "jev_malformed"


@dataclass(frozen=True)
class JudgeResult:
    model: str  # exact versioned ID that answered, e.g. jev-1.13.0
    answers: dict
    usage: dict | None
    request_id: str | None
    latency_s: float


class JudgeClient:
    def __init__(
        self,
        url: str,
        desk_secret: str,
        backoff_initial_s: float = 0.25,
        backoff_max_s: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not desk_secret:
            raise ValueError("DESK_JUDGE_SECRET is not set")
        self._url = url.rstrip("/") + "/v1/judge"
        self._secret = desk_secret
        self._b0, self._bmax = backoff_initial_s, backoff_max_s
        self._clock, self._sleep = clock, sleep

    def ask(self, state: dict | str, questions: dict, deadline: float) -> JudgeResult:
        """Ask a whole battery in one call. `deadline` is on this client's clock."""
        body = json.dumps({"state": state, "questions": questions}).encode()
        attempt = 0
        start = self._clock()
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise JudgeDeadline("decision deadline passed before the judge answered")
            req = urllib.request.Request(
                self._url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._secret}", "X-Timeout-S": f"{remaining:.3f}"},
            )
            retry_after = None
            try:
                with urllib.request.urlopen(req, timeout=remaining) as resp:
                    payload = json.loads(resp.read())
                    return JudgeResult(payload["model"], payload["answers"], payload.get("usage"), payload.get("request_id"), self._clock() - start)
            except urllib.error.HTTPError as e:
                err = _error_body(e)
                if e.code == 400 and err.get("error") == "malformed_question":
                    raise JudgeMalformed(err.get("detail", "malformed question")) from None
                if e.code == 429:
                    ms = err.get("retry_after_ms")
                    retry_after = ms / 1000 if isinstance(ms, (int, float)) else None
                    failure: JudgeError = JudgeRateLimited("rate limited", retry_after)
                elif e.code in (502, 503, 504):
                    failure = JudgeError(f"judge returned {e.code}: {err.get('error')}")
                else:
                    raise JudgeError(f"judge returned {e.code}: {err.get('error')}") from None
            except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
                if deadline - self._clock() <= 0:
                    raise JudgeDeadline("decision deadline passed before the judge answered") from None
                failure = JudgeError(f"judge unreachable: {type(e).__name__}")

            attempt += 1
            wait = retry_after if retry_after is not None else min(self._bmax, self._b0 * 2 ** (attempt - 1)) * (1 - 0.25 * random.random())
            if self._clock() + wait >= deadline:
                raise failure
            self._sleep(wait)


def _error_body(e: urllib.error.HTTPError) -> dict:
    try:
        body = json.loads(e.read() or b"{}")
        return body if isinstance(body, dict) else {}
    except (ValueError, OSError):
        return {}
