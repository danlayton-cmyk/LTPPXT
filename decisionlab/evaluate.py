"""Model-agnostic forecast evaluation. Knows nothing about markets, Jev or the desk.

A forecaster is anything that turns rows into probabilities for a binary target. This
module owns the parts that must not change when a model or strategy changes: the
chronological split, the scores, and the uncertainty around a score difference.
Research code may import it; it must not import research code.

Requires numpy only.
"""

import math

import numpy as np


def purged_chronological_split(times, fractions=(0.6, 0.2, 0.2), purge=0.0):
    """Index arrays for consecutive time-ordered folds, e.g. train / validation / test.

    `times` are label start times in any numeric unit. Rows that share a timestamp (several
    symbols at one decision time) always land in the same fold. `purge`, in the same unit,
    is dropped from the start of every fold after the first: set it to the label horizon
    plus the longest feature lookback, so no later fold overlaps an earlier fold's labels.
    """
    times = np.asarray(times, dtype=float)
    if abs(sum(fractions) - 1.0) > 1e-9 or any(f <= 0 for f in fractions):
        raise ValueError("fractions must be positive and sum to 1")
    if purge < 0 or not np.isfinite(times).all():
        raise ValueError("purge must be nonnegative and times finite")
    uniq = np.unique(times)
    if len(uniq) < len(fractions):
        raise ValueError("fewer distinct timestamps than folds")
    edges = [uniq[min(len(uniq) - 1, int(round(len(uniq) * c)))] for c in np.cumsum(fractions)[:-1]]
    folds, lo = [], -math.inf
    for k, hi in enumerate([*edges, math.inf]):
        start = lo if k == 0 else lo + purge
        folds.append(np.flatnonzero((times >= start) & (times < hi)))
        lo = hi
    return folds


def brier(p, y) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2))


def log_loss(p, y, eps: float = 1e-6) -> float:
    p, y = np.clip(np.asarray(p, float), eps, 1 - eps), np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def auc(p, y) -> float | None:
    """Mann-Whitney AUC with midranks for ties. None when one class is absent."""
    p, y = np.asarray(p, float), np.asarray(y, bool)
    pos, neg = int(y.sum()), int((~y).sum())
    if not pos or not neg:
        return None
    order = np.argsort(p, kind="mergesort")
    sorted_p = p[order]
    ranks = np.empty(len(p))
    i = 0
    while i < len(p):
        j = i
        while j + 1 < len(p) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[y].sum() - pos * (pos + 1) / 2) / (pos * neg))


def block_bootstrap_mean(values, blocks, n_boot: int = 2000, seed: int = 0, alpha: float = 0.05) -> dict:
    """Mean of `values` with a percentile interval from resampling whole blocks.

    `blocks` labels each row with its block (e.g. the UTC day). Everything in a block moves
    together, so correlation inside a block (overlapping windows, several symbols at one
    time) does not shrink the interval. `p_le_zero` is the share of resamples whose mean is
    <= 0, and `p_ge_zero` the share >= 0: one-sided bootstrap p-values for "the mean is
    positive" and "the mean is negative".
    """
    values, blocks = np.asarray(values, float), np.asarray(blocks)
    if len(values) == 0 or len(values) != len(blocks):
        raise ValueError("values and blocks must be the same nonzero length")
    _, inv = np.unique(blocks, return_inverse=True)
    sums = np.bincount(inv, weights=values)
    counts = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(sums), size=(n_boot, len(sums)))
    means = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
    return {
        "mean": float(values.mean()),
        "lo": float(np.quantile(means, alpha / 2)),
        "hi": float(np.quantile(means, 1 - alpha / 2)),
        "p_le_zero": float((means <= 0).mean()),
        "p_ge_zero": float((means >= 0).mean()),
        "n": int(len(values)),
        "n_blocks": int(len(sums)),
    }


def brier_difference(p_model, p_base, y, blocks, **kw) -> dict:
    """Brier(model) - Brier(base) with a block-bootstrap interval. Negative favors the model."""
    p_model, p_base, y = (np.asarray(a, float) for a in (p_model, p_base, y))
    return block_bootstrap_mean((p_model - y) ** 2 - (p_base - y) ** 2, blocks, **kw)


def holm(p_values: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values for a family of tests. Use over every cell an
    experiment looked at, not only the ones that came out well."""
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    out, running, m = {}, 0.0, len(ordered)
    for i, (k, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


class Logistic:
    """L2-regularized logistic regression, Newton's method, standardized features.
    Deterministic: no random state. The intercept is not penalized."""

    def __init__(self, l2: float = 1.0, iters: int = 50) -> None:
        self.l2, self.iters = l2, iters
        self.mu = self.sd = self.w = None

    def _design(self, x):
        z = (np.asarray(x, float) - self.mu) / self.sd
        return np.hstack([np.ones((len(z), 1)), z])

    def fit(self, x, y) -> "Logistic":
        x, y = np.asarray(x, float), np.asarray(y, float)
        self.mu, self.sd = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-12)
        d = self._design(x)
        w = np.zeros(d.shape[1])
        pen = np.full(d.shape[1], self.l2)
        pen[0] = 0.0
        for _ in range(self.iters):
            p = 1 / (1 + np.exp(-np.clip(d @ w, -30, 30)))
            grad = d.T @ (p - y) + pen * w
            hess = (d * (p * (1 - p))[:, None]).T @ d + np.diag(pen) + 1e-9 * np.eye(len(w))
            step = np.linalg.solve(hess, grad)
            w = w - step
            if np.max(np.abs(step)) < 1e-8:
                break
        self.w = w
        return self

    def predict(self, x):
        return 1 / (1 + np.exp(-np.clip(self._design(x) @ self.w, -30, 30)))
