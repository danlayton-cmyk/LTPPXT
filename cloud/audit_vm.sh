#!/usr/bin/env bash
# Read-only cost check for the project that runs the desk. Changes nothing.
#
#   PROJECT=my-project bash cloud/audit_vm.sh
#
# Checks each VM against the Compute Engine free tier (Google's Free Tier table, page
# last updated 2026-10-27): one non-preemptible e2-micro per billing account per month,
# only in us-west1, us-central1 or us-east1, 30 GB-months of *standard* persistent disk,
# 1 GB/month egress from North America. It also lists things that bill while idle:
# external IPv4 addresses, reserved-but-unused static IPs, snapshots, and whether a budget exists.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT" ] || { echo "set PROJECT=<project id>" >&2; exit 1; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

gcloud compute instances list --project "$PROJECT" --format=json >"$tmp/instances.json"
gcloud compute disks list --project "$PROJECT" --format=json >"$tmp/disks.json"
gcloud compute addresses list --project "$PROJECT" --format=json >"$tmp/addresses.json"
gcloud compute snapshots list --project "$PROJECT" --format=json >"$tmp/snapshots.json" 2>/dev/null || echo '[]' >"$tmp/snapshots.json"
billing="$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)' 2>/dev/null || true)"
budgets="unknown (no permission to list budgets, or billing API off)"
if [ -n "$billing" ]; then
  if out="$(gcloud billing budgets list --billing-account "${billing#billingAccounts/}" --format='value(name)' 2>/dev/null)"; then
    budgets="$(printf '%s' "$out" | grep -c . || true) budget(s) on ${billing#billingAccounts/}"
  fi
fi

python3 - "$tmp" "$PROJECT" "$budgets" <<'EOF'
import json, sys
from pathlib import Path
tmp, project, budgets = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
inst = json.loads((tmp / "instances.json").read_text())
disks = json.loads((tmp / "disks.json").read_text())
addrs = json.loads((tmp / "addresses.json").read_text())
snaps = json.loads((tmp / "snapshots.json").read_text())
FREE_REGIONS = {"us-west1", "us-central1", "us-east1"}
last = lambda url: (url or "").rsplit("/", 1)[-1]
flags = []

print(f"Project {project}: {len(inst)} VM(s)\n")
micro_running = 0
for i in inst:
    zone = last(i["zone"]); region = zone.rsplit("-", 1)[0]
    mtype = last(i["machineType"])
    sched = i.get("scheduling", {})
    spot = sched.get("preemptible") or sched.get("provisioningModel") == "SPOT"
    ext = [ac.get("natIP") for ni in i.get("networkInterfaces", []) for ac in ni.get("accessConfigs", []) if ac.get("natIP")]
    sas = i.get("serviceAccounts", [])
    print(f"- {i['name']}  zone={zone}  type={mtype}  status={i['status']}  labels={i.get('labels', {})}")
    print(f"    external IPv4: {', '.join(ext) or 'none'}")
    for sa in sas:
        scopes = [s.rsplit('/', 1)[-1] for s in sa.get("scopes", [])]
        print(f"    service account: {sa['email']}  scopes: {', '.join(scopes)}")
    if mtype == "e2-micro" and not spot and i["status"] == "RUNNING":
        micro_running += 1
    if mtype != "e2-micro":
        flags.append(f"{i['name']}: {mtype} is not the free e2-micro")
    if spot:
        flags.append(f"{i['name']}: Spot/preemptible VMs are not covered by the e2-micro free tier")
    if region not in FREE_REGIONS:
        flags.append(f"{i['name']}: region {region} is outside the free-tier regions {sorted(FREE_REGIONS)}")
    if ext:
        flags.append(f"{i['name']}: has an external IPv4. Google's VPC pricing lists ~$0.005/hour (~$3.65/month) for one on a "
                     "standard VM (research source; confirm on your bill). The desk needs outbound internet, so removing it "
                     "means Cloud NAT, which is also billed: compare before changing.")
    if any(sa["email"].endswith("-compute@developer.gserviceaccount.com") for sa in sas):
        flags.append(f"{i['name']}: uses the default Compute Engine service account, which often has the project Editor role. "
                     "cloud/setup_bigquery.sh creates a narrow one.")
if micro_running > 1:
    flags.append(f"{micro_running} e2-micro VMs running: the free allowance is one VM's worth of hours per month in total")

std_gb = 0
print()
for d in disks:
    dtype = last(d.get("type")); size = int(d.get("sizeGb", 0))
    attached = "attached" if d.get("users") else "UNATTACHED"
    print(f"- disk {d['name']}  {dtype}  {size} GB  {attached}")
    if dtype == "pd-standard":
        std_gb += size
    else:
        flags.append(f"disk {d['name']}: {dtype} is billed; only pd-standard is in the free tier")
    if not d.get("users"):
        flags.append(f"disk {d['name']}: unattached, still billed for storage")
if std_gb > 30:
    flags.append(f"{std_gb} GB of standard disk; the free tier covers 30 GB-months")

for a in addrs:
    if a.get("status") == "RESERVED":
        flags.append(f"static IP {a['name']} ({a.get('address')}) is reserved but unused; unused static IPs are billed")
if snaps:
    flags.append(f"{len(snaps)} snapshot(s) exist; snapshot storage is billed")
print(f"\nBudgets: {budgets}")
if budgets.startswith("0 "):
    flags.append("no budget on the billing account: run cloud/setup_guardrails.sh")

print("\nFindings:" if flags else "\nFindings: none. Matches the free-tier shape (still check the Billing report).")
for f in flags:
    print(f"  * {f}")
print("\nEgress is not visible from here. On the VM, run: ltppxt egress")
EOF
