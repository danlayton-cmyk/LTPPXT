#!/usr/bin/env bash
# Budget alert + automatic VM stop for the desk's project. Run in Cloud Shell as someone who
# can manage the billing account's budgets and the project's IAM:
#
#   PROJECT=my-project VM=ltppxt ZONE=us-central1-a bash cloud/setup_guardrails.sh
#
# What it sets up:
#   1. A monthly budget scoped to this project ($BUDGET_USD, default 10) that emails the
#      billing admins at 50%, 90% and 100% of actual spend, and publishes every update to
#      a Pub/Sub topic.
#   2. A Cloud Run function (cloud/budget_guard) on that topic that STOPS VMs labelled
#      ltppxt=desk once actual spend reaches STOP_AT_FRACTION (default 1.0) of the budget.
#      It never deletes anything and never disables billing.
#   3. The label ltppxt=desk on $VM, so the guard knows which VM to stop.
#
# Why not the "disable billing" kill switch: Google documents that it shuts off every
# service in the project, free ones included, and resources can be deleted afterwards.
# Stopping the VM is the gentler pattern Google also documents.
#
# Why $10 and not $5: if your bill shows the external IPv4 charge (~$3.65/month per Google's
# VPC pricing) plus some egress, a $5 line could stop the desk late every month. Run
# cloud/audit_vm.sh and look at last month's bill, then set BUDGET_USD to something above
# normal spend.
#
# Deploying the function uses Cloud Build and Artifact Registry. Their free tiers (2,500
# build-minutes, 0.5 GB storage) should cover one small function; confirm in the console.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
VM="${VM:-}"
ZONE="${ZONE:-}"
REGION="${REGION:-us-central1}"
BUDGET_USD="${BUDGET_USD:-10}"
STOP_AT_FRACTION="${STOP_AT_FRACTION:-1.0}"
BUDGET_NAME="${BUDGET_NAME:-ltppxt-monthly}"
TOPIC="${TOPIC:-ltppxt-budget}"
FN="${FN:-ltppxt-budget-guard}"
SA_NAME="${SA_NAME:-ltppxt-guard}"
ROLE_ID="${ROLE_ID:-ltppxtVmStopper}"
DRY_RUN="${DRY_RUN:-1}"  # first deploy logs only; re-run with DRY_RUN=0 to arm it
HERE="$(cd "$(dirname "$0")" && pwd)"

die() { echo "setup_guardrails: $*" >&2; exit 1; }
[ -n "$PROJECT" ] || die "set PROJECT=<project id>"
billing="$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)')"
[ -n "$billing" ] || die "project $PROJECT has no billing account linked"
billing="${billing#billingAccounts/}"
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"

echo "Enabling APIs..."
gcloud services enable --project "$PROJECT" \
  billingbudgets.googleapis.com pubsub.googleapis.com compute.googleapis.com \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com eventarc.googleapis.com

if [ -n "$VM" ]; then
  [ -n "$ZONE" ] || die "set ZONE for $VM"
  gcloud compute instances add-labels "$VM" --zone "$ZONE" --project "$PROJECT" --labels ltppxt=desk
  echo "Labelled $VM with ltppxt=desk."
else
  echo "VM not set: label your desk VM yourself (ltppxt=desk) or the guard will find nothing to stop."
fi

gcloud pubsub topics describe "$TOPIC" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub topics create "$TOPIC" --project "$PROJECT"

if gcloud billing budgets list --billing-account "$billing" --format='value(displayName)' | grep -qx "$BUDGET_NAME"; then
  echo "Budget $BUDGET_NAME already exists; leaving it as is."
else
  gcloud billing budgets create --billing-account "$billing" \
    --display-name "$BUDGET_NAME" \
    --budget-amount "${BUDGET_USD}USD" \
    --calendar-period month \
    --filter-projects "projects/$PROJECT" \
    --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0 \
    --notifications-rule-pubsub-topic "projects/$PROJECT/topics/$TOPIC"
  echo "Budget $BUDGET_NAME: \$$BUDGET_USD/month on $PROJECT."
fi

# Least privilege: list and stop VMs, nothing else.
if ! gcloud iam roles describe "$ROLE_ID" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam roles create "$ROLE_ID" --project "$PROJECT" \
    --title "LTPPXT VM stopper" --description "List and stop VMs; used by the budget guard" \
    --permissions compute.instances.list,compute.instances.get,compute.instances.stop --stage GA
fi
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "LTPPXT budget guard"
gcloud projects add-iam-policy-binding "$PROJECT" --condition=None --quiet \
  --member "serviceAccount:$SA" --role "projects/$PROJECT/roles/$ROLE_ID" >/dev/null

echo "Deploying $FN (DRY_RUN=$DRY_RUN)..."
gcloud functions deploy "$FN" --gen2 --project "$PROJECT" --region "$REGION" \
  --runtime python312 --source "$HERE/budget_guard" --entry-point on_budget \
  --trigger-topic "$TOPIC" \
  --service-account "$SA" --trigger-service-account "$SA" \
  --memory 256Mi --max-instances 1 --no-allow-unauthenticated \
  --set-env-vars "GUARD_PROJECT=$PROJECT,STOP_AT_FRACTION=$STOP_AT_FRACTION,BUDGET_NAME=$BUDGET_NAME,LABEL_KEY=ltppxt,LABEL_VALUE=desk,DRY_RUN=$DRY_RUN"
# The Pub/Sub trigger calls the function as $SA, so $SA must be allowed to invoke it.
gcloud functions add-invoker-policy-binding "$FN" --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:$SA" >/dev/null

cat <<EOF

Done. Test it without spending anything (with DRY_RUN=1 nothing is stopped; with DRY_RUN=0 this WILL stop the VM):

  gcloud pubsub topics publish $TOPIC --project $PROJECT \\
    --message '{"budgetDisplayName":"$BUDGET_NAME","costAmount":999,"budgetAmount":$BUDGET_USD,"currencyCode":"USD"}'
  gcloud functions logs read $FN --project $PROJECT --region $REGION --limit 20

Re-run this script with DRY_RUN=0 when the dry run logs the VM you expect.
EOF
