"""Sample and confidence gates for local DecisionLab evaluation."""

from numbers import Integral


def evaluate_decision_policy(confidence_score, threshold=0.65, min_sample_size=100, *, sample_size=None):
    """Require an explicit out-of-sample count before passing the confidence gate."""
    if isinstance(min_sample_size, bool) or not isinstance(min_sample_size, Integral) or min_sample_size < 1:
        raise ValueError("min_sample_size must be a positive integer")
    if sample_size is not None and (
        isinstance(sample_size, bool) or not isinstance(sample_size, Integral) or sample_size < 0
    ):
        raise ValueError("sample_size must be a nonnegative integer")
    if not 0 <= confidence_score <= 1 or not 0 <= threshold <= 1:
        raise ValueError("confidence_score and threshold must be between 0 and 1")

    result = {"action": "HOLD", "confidence": confidence_score}
    if sample_size is None or sample_size < min_sample_size:
        return {**result, "gate": "FAIL_SAMPLE_SIZE"}
    if confidence_score < threshold:
        return {**result, "gate": "FAIL_THRESHOLD"}
    return {**result, "action": "EXECUTE", "gate": "PASS"}
