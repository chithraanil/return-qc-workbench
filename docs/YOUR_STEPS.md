# Your steps (things only you can do, because they need your identity)

Everything else (code, tests, deploy scripts, ServiceNow configuration, demo data) is built.
Total time: about 60–75 min. Do them in this order.

| # | Step | Time | Why it must be you |
|---|---|---|---|
| 1 | ServiceNow: **Request instance** at developer.servicenow.com → note the URL | 5 min | Your ServiceNow ID |
| 2 | Argolis: **grant access** (choose 2A or 2B) | 5 min | Org admin |
| 3 | Argolis: **org policies** + workforce pool | 10 min | Org admin |
| 4 | Run **ServiceNow setup script** + set 2 passwords in the ServiceNow UI | 10 min | Admin password stays with you |
| 5 | **Deploy** (one script) | 10 min | Runs as you (or me, after 2A) |
| 6 | GE console: **OAuth client + connectors** | 15 min | Consent/OAuth tied to your identity |
| 7 | GE: **build the workflow** (Workflow Builder UI) | 10 min | No public API for Workflow Builder |
| 8 | **Shoot photos/videos** per `data/SHOT_LIST.md` | 60–90 min | Physical items |

---

## 1. ServiceNow instance
developer.servicenow.com → **Request instance** (top right) → latest release → wait → profile → **Manage instance password**. Keep the admin password private; send me only the URL.

## 2. Access for the agent (choose one)
**2A (I do the deploys and tests):**
```bash
P=agent-builder-ge; SA=capsule-sa-ar@capsules-agents-1.iam.gserviceaccount.com
for R in roles/aiplatform.user roles/run.admin roles/storage.admin roles/secretmanager.admin \
  roles/iam.serviceAccountAdmin roles/iam.serviceAccountUser roles/cloudbuild.builds.editor \
  roles/artifactregistry.admin roles/serviceusage.serviceUsageAdmin roles/resourcemanager.projectIamAdmin; do
  gcloud projects add-iam-policy-binding $P --member="serviceAccount:$SA" --role="$R" --condition=None
done
```
If it fails with a domain error: add a project-level exception to `iam.allowedPolicyMemberDomains` for that account.
Revoke after the demo with the same loop, using `remove-iam-policy-binding`.

**2B (you run everything):** skip; run steps 4–5 yourself in Cloud Shell.

## 3. Org policies (Argolis org or `agent-builder-ge` project)
- `disableCustomMcpServerConnector` → **not enforced** (search for the name in *IAM & Admin → Organization Policies*)
- Confirm a **workforce identity pool** exists (needed by the GE ServiceNow connector)

## 4. Configure ServiceNow (script does ~40 config items for you)
```bash
cd qc_demo && pip install -r requirements.txt
export SN_INSTANCE=https://devXXXXXX.service-now.com SN_USER=admin
read -rs SN_PASSWORD && export SN_PASSWORD          # typed, never pasted into chat
python servicenow/setup_instance.py configure --analyst-email <your GE login email>
python servicenow/setup_instance.py seed            # 19 claim tickets + customer photos
```
Then in ServiceNow UI: **User Administration → Users** → `qc.agent.integration` → *Set Password* (save it for step 5), and the same for `qc.analyst`.
Add the **Return QC** section to the Incident form (*Configure → Form Layout*) so the fields show in the demo.

## 5. Deploy the Workbench
```bash
cd qc_demo
SN_INSTANCE=https://devXXXXXX.service-now.com ./deploy/deploy.sh
```
It prompts once (hidden input) for the `qc.agent.integration` password and stores it in Secret Manager.
Output: `https://qc-workbench-xxxx.run.app/mcp`.

## 6. Gemini Enterprise console
**6a. OAuth client** (APIs & Services → Credentials → Create OAuth client ID → *Web application*):
- Redirect URI: `https://vertexaisearch.cloud.google.com/oauth-redirect`
- Then redeploy with: `OAUTH_CLIENT_ID=<client id> ./deploy/deploy.sh`

**6b. Workbench connector** (GE → your app → Data stores → Create → *Custom MCP server*):
| Field | Value |
|---|---|
| MCP Server URL | `https://qc-workbench-xxxx.run.app/mcp` |
| Authorization URL | `https://accounts.google.com/o/oauth2/v2/auth` |
| Token URL | `https://oauth2.googleapis.com/token` |
| Client ID / Secret | from 6a |
| Scopes | `openid email profile` |
| PKCE | **ON** |
| HTTP Basic Auth | **OFF** (on causes `invalid_client`) |
Actions tab → **Reload** → enable `open_qc_workbench`, `analyze_claim`, `apply_decision`.
⚠️ Attach under **Data Stores** to the app; do **not** use "Add Agent" (it can't mount the side panel).

**6c. ServiceNow connector**: follow Steps 5–6 of `servicenow_setup_guide.md` (OAuth app in ServiceNow, data store in GE, enable *Update incident / Add ticket comment / Upload attachment*).

**6d. Smoke test:** in GE chat type **"Open the Return QC Workbench"** → the side panel opens → *All claims* → **Analyze pending**.

## 7. Workflow
Follow `docs/WORKFLOW_GUIDE.md` (about 10 clicks).

## 8. Photos/videos
`data/SHOT_LIST.md`: save into `data/media/` with the exact names, then redeploy (`./deploy/deploy.sh`).
