"""Budget guard: stop the desk's VM when this month's actual spend reaches the budget line.

Deployed by cloud/setup_guardrails.sh as a Cloud Run function on the budget's Pub/Sub topic.
It STOPS labelled VMs; it does not delete anything and does not disable billing, so disks,
logs and the free-tier resources elsewhere in the project are left alone. Start the VM
again by hand (gcloud compute instances start ...) once you have looked at the bill.

Google's documented caveats apply: budget notifications lag actual spend (often by hours),
so spend can pass the line before this runs.

Environment:
  GUARD_PROJECT        project whose VMs it may stop (required)
  STOP_AT_FRACTION     fraction of the budget amount that triggers a stop (default 1.0)
  BUDGET_NAME          only act on notifications from this budget display name (recommended)
  LABEL_KEY/LABEL_VALUE  which VMs to stop (default ltppxt=desk)
  DRY_RUN=1            log what it would stop, stop nothing
"""

import base64
import os

import functions_framework
from google.cloud import compute_v1

from decide import instances_to_stop, parse_budget_message, should_stop


@functions_framework.cloud_event
def on_budget(cloud_event) -> None:
    raw = base64.b64decode(cloud_event.data["message"]["data"])
    msg = parse_budget_message(raw)
    stop, why = should_stop(msg, float(os.environ.get("STOP_AT_FRACTION", "1.0")), os.environ.get("BUDGET_NAME") or None)
    print(f"budget_guard: {why}")
    if not stop:
        return

    project = os.environ["GUARD_PROJECT"]
    key, value = os.environ.get("LABEL_KEY", "ltppxt"), os.environ.get("LABEL_VALUE", "desk")
    client = compute_v1.InstancesClient()
    found = []
    for zone_key, scoped in client.aggregated_list(project=project):
        for inst in scoped.instances or []:
            found.append({"name": inst.name, "zone": zone_key.rsplit("/", 1)[-1], "status": inst.status, "labels": dict(inst.labels)})
    targets = instances_to_stop(found, key, value)
    if not targets:
        print(f"budget_guard: no RUNNING VM labelled {key}={value} in {project}")
        return
    for t in targets:
        if os.environ.get("DRY_RUN") == "1":
            print(f"budget_guard: DRY_RUN, would stop {t['name']} in {t['zone']}")
            continue
        client.stop(project=project, zone=t["zone"], instance=t["name"])
        print(f"budget_guard: stop requested for {t['name']} in {t['zone']}")
