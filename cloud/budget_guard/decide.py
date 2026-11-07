"""Pure decision logic for the budget guard, kept free of Google imports so it tests offline."""

import json


def parse_budget_message(raw: bytes | str) -> dict:
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        raise ValueError("budget notification is not a JSON object")
    return msg


def should_stop(msg: dict, stop_at_fraction: float, expected_budget_name: str | None) -> tuple[bool, str]:
    """Stop only on *actual* cost at or past the line, never on forecast.

    Cloud Billing sends notifications for a budget several times a day whether or not a
    threshold was crossed, with costAmount (spend so far this period) and budgetAmount.
    """
    name = msg.get("budgetDisplayName")
    if expected_budget_name and name != expected_budget_name:
        return False, f"ignoring budget {name!r}; this guard acts on {expected_budget_name!r} only"
    try:
        cost = float(msg["costAmount"])
        budget = float(msg["budgetAmount"])
    except (KeyError, TypeError, ValueError):
        return False, "notification has no usable costAmount/budgetAmount"
    if budget <= 0 or stop_at_fraction <= 0:
        return False, "budget or stop fraction is not positive; refusing to act"
    line = budget * stop_at_fraction
    if cost >= line:
        return True, f"cost {cost:.2f} >= {line:.2f} ({stop_at_fraction:.0%} of {budget:.2f} {msg.get('currencyCode', '')})"
    return False, f"cost {cost:.2f} < {line:.2f}"


def instances_to_stop(instances: list[dict], label_key: str, label_value: str) -> list[dict]:
    """instances: [{'name', 'zone', 'status', 'labels'}]. Only RUNNING VMs carrying the label."""
    return [i for i in instances if i.get("status") == "RUNNING" and (i.get("labels") or {}).get(label_key) == label_value]
