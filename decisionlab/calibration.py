"""Local CPU calibration audit for COUNCIL-20261109-003."""

import math

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss


def purged_walk_forward_split(df, time_col="timestamp", horizon_sec=1800, purge_sec=1800, train_ratio=0.7):
    """Return one chronological holdout as positions into the original frame."""
    if not 0 < train_ratio < 1:
        raise ValueError("train_ratio must be between 0 and 1")
    if any(not math.isfinite(seconds) or seconds < 0 for seconds in (horizon_sec, purge_sec)):
        raise ValueError("horizon_sec and purge_sec must be finite and nonnegative")

    times = df[time_col].reset_index(drop=True)
    if times.empty:
        return np.empty(0, dtype=int), np.empty(0, dtype=int)
    if not pd.api.types.is_datetime64_any_dtype(times) or times.isna().any():
        raise ValueError(f"{time_col} must contain valid datetime values")

    ordered = times.sort_values(kind="stable")
    train_count = int(len(ordered) * train_ratio)
    if train_count == 0:
        return np.empty(0, dtype=int), ordered.index.to_numpy()

    train_end = ordered.iloc[train_count - 1]
    test_start = train_end + pd.Timedelta(seconds=horizon_sec + purge_sec)
    # Keep equal timestamps together, even with a zero-second gap.
    train = ordered[ordered <= train_end]
    test = ordered[(ordered >= test_start) & (ordered > train_end)]
    return train.index.to_numpy(), test.index.to_numpy()


def run_calibration_audit(df, feature_cols, target_col, time_col="timestamp"):
    """Compare train-fit logistic regression and prevalence on held-out rows."""
    train_idx, test_idx = purged_walk_forward_split(df, time_col)
    labels = df[target_col]
    features = df[list(feature_cols)]
    y_train, y_test = labels.iloc[train_idx], labels.iloc[test_idx]

    result = {
        "verdict": "REVISE/NO-GO",
        "brier_constant": None,
        "brier_causal": None,
        "delta": None,
        "train_samples": len(train_idx),
        "test_samples": len(test_idx),
    }
    if y_train.empty or y_test.empty:
        return {**result, "reason": "empty_train_or_test_slice"}
    if not y_train.isin([0, 1]).all() or not y_test.isin([0, 1]).all():
        raise ValueError(f"{target_col} must contain binary labels (0 or 1)")

    y_train = y_train.astype(int)
    y_test = y_test.astype(int)
    constant = np.full(len(y_test), y_train.mean())
    result["brier_constant"] = float(brier_score_loss(y_test, constant))
    if y_train.nunique() == 1:
        return {**result, "reason": "single_class_training_target"}

    model = LogisticRegression(solver="liblinear", random_state=0)
    model.fit(features.iloc[train_idx], y_train)
    predicted = model.predict_proba(features.iloc[test_idx])[:, 1]
    result["brier_causal"] = float(brier_score_loss(y_test, predicted))
    result["delta"] = result["brier_causal"] - result["brier_constant"]
    if result["delta"] < 0:
        result["verdict"] = "GO"
    return result
