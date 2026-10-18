from desk.config import PINNED_JEV_MODEL
from judge.client import JudgeResult
from state.snapshot import Snapshot, Tick


def make_snap(**over) -> Snapshot:
    base = dict(
        symbol="BTC-USD", as_of=1000.0, data_ts=999.0, mid=100.0, spread_bps=2.0, imbalance=0.3,
        rvol_bps=60.0, returns_bps={"1m": 5.0, "5m": 10.0, "15m": 20.0}, inventory=0.0, drawdown_pct=0.0,
    )
    base.update(over)
    return Snapshot(**base)


def ticks(symbol="BTC-USD", start=0.0, n=1200, every=1.0, price=100.0, step=0.0, spread=0.02, size=50.0):
    out = []
    for k in range(n):
        p = price + step * k
        out.append(Tick(start + k * every, symbol, p - spread / 2, p + spread / 2, size, size))
    return out


class StubJudge:
    """Returns fixed raw answers; each call can override the model string."""

    def __init__(self, answers: dict, model: str = PINNED_JEV_MODEL) -> None:
        self.answers, self.model, self.calls = answers, model, 0

    def ask(self, state, questions, deadline):
        self.calls += 1
        missing = set(questions) - set(self.answers)
        assert not missing, f"stub has no answer for {missing}"
        return JudgeResult(self.model, {q: self.answers[q] for q in questions}, {"input_tokens": 1, "output_tokens": 0}, "req-1", 0.01)


def choice(probs: dict) -> dict:
    top = max(probs, key=probs.get)
    return {"type": "choice", "choice": top, "confidence": 0.5, "probabilities": probs}


def noul(p: float) -> dict:
    return {"type": "noul", "noul": p}


def score(probs: list[float]) -> dict:
    return {"type": "score", "score": sum(i * p for i, p in enumerate(probs)), "confidence": 0.5,
            "legend": {str(i): "x" for i in range(len(probs))}, "probabilities": {str(i): p for i, p in enumerate(probs)}}


BULLISH_GAMBLER = {
    "regime": choice({"trending_up": 0.9, "trending_down": 0.02, "ranging": 0.04, "disorderly": 0.02, "other": 0.02}),
    "direction": choice({"up": 0.95, "down": 0.02, "flat": 0.02, "other": 0.01}),
    "chase_risk": score([0.9, 0.1, 0.0, 0.0]),
    "exit_now": noul(0.1),
}
