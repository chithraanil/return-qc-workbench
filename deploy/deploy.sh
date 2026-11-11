#!/usr/bin/env bash
# Deploys the Return QC Workbench (private Cloud Run) into agent-builder-ge.
# Run from qc_demo/ as a user with project Owner/Editor (or the granted agent SA).
#   ./deploy/deploy.sh                # deploy / redeploy
#   SN_INSTANCE=https://devXXXX.service-now.com ./deploy/deploy.sh   # with ServiceNow
set -euo pipefail

PROJECT=${PROJECT:-agent-builder-ge}
REGION=${REGION:-us-central1}
SERVICE=${SERVICE:-qc-workbench}
RUNTIME_SA_NAME=qc-workbench-runtime
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
BUCKET="gs://${PROJECT}-qc-demo"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
GE_P4SA="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"

echo "== 1. APIs"
gcloud services enable aiplatform.googleapis.com run.googleapis.com discoveryengine.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  storage.googleapis.com iamcredentials.googleapis.com --project "$PROJECT"

echo "== 2. Runtime service account (least privilege; never acts as the user)"
gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" --project "$PROJECT" \
    --display-name "QC Workbench runtime"
for R in roles/aiplatform.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$RUNTIME_SA" \
    --role "$R" --condition=None --quiet >/dev/null
done

echo "== 3. Bucket for media/evidence"
gcloud storage buckets describe "$BUCKET" >/dev/null 2>&1 || \
  gcloud storage buckets create "$BUCKET" --project "$PROJECT" --location "$REGION" \
    --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member "serviceAccount:$RUNTIME_SA" --role roles/storage.objectAdmin >/dev/null

echo "== 4. ServiceNow secret (optional)"
SECRET_FLAGS=""
ENV_SN=""
if [[ -n "${SN_INSTANCE:-}" ]]; then
  if ! gcloud secrets describe sn-password --project "$PROJECT" >/dev/null 2>&1; then
    read -rsp "ServiceNow password for qc.agent.integration (input hidden): " SNPW; echo
    printf '%s' "$SNPW" | gcloud secrets create sn-password --project "$PROJECT" \
      --replication-policy automatic --data-file=-
    unset SNPW
  fi
  gcloud secrets add-iam-policy-binding sn-password --project "$PROJECT" \
    --member "serviceAccount:$RUNTIME_SA" --role roles/secretmanager.secretAccessor >/dev/null
  SECRET_FLAGS="--set-secrets=SN_PASSWORD=sn-password:latest"
  ENV_SN=",SN_INSTANCE=${SN_INSTANCE},SN_USER=${SN_USER:-qc.agent.integration}"
fi

echo "== 5. Deploy (private; CPU always on for background Argon jobs)"
gcloud run deploy "$SERVICE" --source . --project "$PROJECT" --region "$REGION" \
  --service-account "$RUNTIME_SA" --no-allow-unauthenticated \
  --no-cpu-throttling --min-instances 1 --max-instances 1 --memory 4Gi --cpu 2 \
  --timeout 300 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=global,REQUIRE_MCP_AUTH=${REQUIRE_MCP_AUTH:-tools},OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID:-}${ENV_SN}" \
  $SECRET_FLAGS

echo "== 6. Only Gemini Enterprise's service agent may invoke"
gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:${GE_P4SA}" --role roles/run.invoker >/dev/null

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
echo
echo "Deployed: ${URL}/mcp"
echo "Next: register it in GE (see docs/YOUR_STEPS.md, step 6)."
# Note: max-instances=1 keeps the in-memory job store consistent for the demo.
# Production: move jobs/verdicts to Firestore and raise max-instances.
