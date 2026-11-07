#!/usr/bin/env bash
# One-time BigQuery setup for the LTPPXT log export. Run it in Cloud Shell (or anywhere
# gcloud and bq are signed in as a project owner):
#
#   PROJECT=my-project bash cloud/setup_bigquery.sh
#
# Creates, if missing:
#   - dataset $DATASET in $LOCATION
#   - table $DATASET.events, partitioned by the `day` column and clustered by type, bot
#   - views: decisions, answers, hypothetical_trades, rejections, daily
#   - service account ltppxt-vm@ with only: BigQuery Job User on the project, and
#     BigQuery Data Editor on this one dataset
# It does NOT touch the VM. Attaching the service account needs a VM stop, so the
# commands are printed at the end for you to run when a restart is acceptable.
#
# Cost: load jobs are free; storage and queries at this volume sit inside BigQuery's
# free 10 GiB storage and 1 TiB query allowance per billing account per month.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
DATASET="${DATASET:-ltppxt}"
LOCATION="${LOCATION:-US}"
SA_NAME="${SA_NAME:-ltppxt-vm}"
HERE="$(cd "$(dirname "$0")" && pwd)"

die() { echo "setup_bigquery: $*" >&2; exit 1; }
[ -n "$PROJECT" ] || die "set PROJECT=<project id> or run: gcloud config set project <id>"
command -v bq >/dev/null || die "bq CLI not found (it comes with the Google Cloud CLI; Cloud Shell has it)"
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"

echo "Project $PROJECT, dataset $DATASET ($LOCATION)"
gcloud services enable bigquery.googleapis.com --project "$PROJECT"

if ! bq --project_id="$PROJECT" show --dataset "$PROJECT:$DATASET" >/dev/null 2>&1; then
  bq --project_id="$PROJECT" --location="$LOCATION" mk --dataset \
    --description "LTPPXT paper desk event logs (simulated trading only)" "$PROJECT:$DATASET"
fi

if ! bq --project_id="$PROJECT" show "$PROJECT:$DATASET.events" >/dev/null 2>&1; then
  bq --project_id="$PROJECT" mk --table \
    --schema "$HERE/schema.json" \
    --time_partitioning_field day --time_partitioning_type DAY \
    --clustering_fields type,bot \
    --description "One row per desk log record. Loaded by cloud/export_bq.py, one partition per UTC day." \
    "$PROJECT:$DATASET.events"
fi

sed "s|@DS@|$PROJECT.$DATASET|g" "$HERE/views.sql" \
  | bq --project_id="$PROJECT" --location="$LOCATION" query --use_legacy_sql=false --quiet >/dev/null
echo "Views created or replaced."

if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name "LTPPXT VM (BigQuery log export only)"
fi
gcloud projects add-iam-policy-binding "$PROJECT" --condition=None --quiet \
  --member "serviceAccount:$SA" --role roles/bigquery.jobUser >/dev/null
# Dataset-level grant, so the VM can write this dataset and nothing else in BigQuery.
bq --project_id="$PROJECT" --location="$LOCATION" query --use_legacy_sql=false --quiet \
  "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`$PROJECT.$DATASET\` TO \"serviceAccount:$SA\"" >/dev/null
echo "Service account $SA: bigquery.jobUser (project), bigquery.dataEditor (dataset $DATASET only)."

cat <<EOF

Done with BigQuery. Two steps left, both on the VM side.

1) Give the VM this service account. This STOPS the VM for about a minute; the desk and
   judge restart on boot. Replace NAME and ZONE (cloud/audit_vm.sh prints them):

   gcloud compute instances stop NAME --zone ZONE
   gcloud compute instances set-service-account NAME --zone ZONE \\
     --service-account $SA --scopes cloud-platform
   gcloud compute instances start NAME --zone ZONE

   The cloud-platform scope is Google's recommended setting; what the VM can actually do
   is limited by the two roles above. This also replaces the default Compute Engine
   service account, which on many projects has the broad Editor role.

2) On the VM, turn on the hourly export (re-running setup.sh updates code and keeps the key):

   curl -fsSL https://raw.githubusercontent.com/danlayton-cmyk/LTPPXT/main/deploy/setup.sh \\
     | sudo LTPPXT_BQ_PROJECT=$PROJECT LTPPXT_BQ_DATASET=$DATASET LTPPXT_BQ_LOCATION=$LOCATION bash
   ltppxt export        # run one export now and show the result

Then point Data Studio (datastudio.google.com) at $PROJECT.$DATASET.daily and
$PROJECT.$DATASET.hypothetical_trades.
EOF
