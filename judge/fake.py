"""Offline stand-in for the judge, for smoke tests without a TypeSafe key.

Answers are deterministic noise shaped like Jev's raw answers. It reports the model as
OFFLINE_FAKE_MODEL, so the model version veto blocks any order built on it, and the
calibration report excludes it.
"""

import hashlib
import json

from desk.config import OFFLINE_FAKE_MODEL
from judge.client import JudgeResult


class FakeJudge:
    def __init__(self, model: str = OFFLINE_FAKE_MODEL) -> None:
        self.model = model
        self.calls = 0

    def ask(self, state, questions: dict, deadline: float) -> JudgeResult:
        self.calls += 1
        seed = json.dumps([state, sorted(questions)], sort_keys=True)
        answers = {qid: _answer(q, f"{seed}:{qid}") for qid, q in questions.items()}
        return JudgeResult(self.model, answers, {"input_tokens": 0, "output_tokens": 0}, None, 0.0)


def _u(seed: str, i: int = 0) -> float:
    return int(hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _dist(seed: str, n: int) -> list[float]:
    w = [_u(seed, i) + 1e-6 for i in range(n)]
    return [x / sum(w) for x in w]


def _answer(q: dict, seed: str) -> dict:
    if q["type"] == "noul":
        return {"type": "noul", "noul": round(_u(seed), 4)}
    if q["type"] == "choice":
        labels = list(q["criteria"])
        probs = dict(zip(labels, _dist(seed, len(labels))))
        top = max(probs, key=probs.get)
        n = len(labels)
        return {"type": "choice", "choice": top, "confidence": (probs[top] - 1 / n) / (1 - 1 / n), "probabilities": probs}
    levels = q["criteria"]
    probs = _dist(seed, len(levels))
    return {
        "type": "score",
        "score": sum(i * p for i, p in enumerate(probs)),
        "confidence": 0.0,
        "legend": {str(i): text for i, text in enumerate(levels)},
        "probabilities": {str(i): p for i, p in enumerate(probs)},
    }
