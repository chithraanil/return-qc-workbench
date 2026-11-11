"""Return QC Workbench: MCP App server for Gemini Enterprise.

Single JSON-RPC 2.0 endpoint at POST /mcp (Streamable HTTP, JSON responses).
Pattern follows go/build-mcp-app-in-ge:
  * UI-launching tools declare multi-dialect UI metadata.
  * Model-visible tools are few; high-frequency UI calls are app-only.
  * Long Argon jobs run in a background pool; the widget polls get_job_state.
Run locally:  uvicorn mcp_server.main:app --port 8080
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import logging
import os
import pathlib
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Optional

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.run import fake_fns, load_claims  # noqa: E402
from qc_core import checks, pipeline  # noqa: E402
from qc_core.schemas import Claim, Media, Route  # noqa: E402

log = logging.getLogger("qc-mcp")
logging.basicConfig(level=logging.INFO)

PROTOCOL_VERSION = "2025-06-18"
UI_URI = "ui://qc-workbench/app.html"
UI_MIME = "text/html;profile=mcp-app"
WIDGET_HTML = (pathlib.Path(__file__).parent / "widget.html").read_text()
REQUIRE_MCP_AUTH = os.environ.get("REQUIRE_MCP_AUTH", "off")  # off | tools | strict
DRY_RUN = os.environ.get("QC_DRY_RUN", "0") == "1"  # local test: fake model outputs
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
OUT_DIR = os.environ.get("QC_OUT_DIR", tempfile.mkdtemp(prefix="qc_out_"))
UPLOAD_DIR = pathlib.Path(tempfile.mkdtemp(prefix="qc_up_"))

RESOURCE_META = {
    "ui": {
        "prefersBorder": False,
        "csp": {"connectDomains": [], "resourceDomains": []},  # fully inline bundle
    }
}


def _ui_meta() -> dict:
  """Multi-dialect UI metadata (ext-apps, SEP-1865 namespaced, GE collector)."""
  return {
      "ui": {"resourceUri": UI_URI, "preferredMode": "pip"},
      "io.modelcontextprotocol/ui": {"resourceUri": UI_URI},
      "ui/resourceUri": UI_URI,
  }


APP_ONLY = {"ui": {"visibility": ["app"]}}

TOOLS = [
    {"name": "open_qc_workbench",
     "description": ("Open the Return QC Workbench side panel for e-commerce return/refund "
                     "fraud review: flagged-claims queue, photo upload, side-by-side "
                     "evidence. Use when the user asks to open the QC workbench, review "
                     "return claims, or compare dispatch vs return photos. Do NOT use canvas."),
     "inputSchema": {"type": "object", "properties": {
         "claim_id": {"type": "string", "description": "Optional claim/ticket to focus"}}},
     "_meta": _ui_meta()},
    {"name": "analyze_claim",
     "description": ("Run QC analysis (Gemini 4 Argon) on one return claim by claim_id "
                     "(e.g. C01) or ServiceNow ticket number (INC...). Returns a job_id; "
                     "results appear in the workbench."),
     "inputSchema": {"type": "object", "required": ["claim_ref"], "properties": {
         "claim_ref": {"type": "string"}}},
     "_meta": _ui_meta()},
    {"name": "apply_decision",
     "description": ("Record the analyst's final decision (APPROVE, REJECT, "
                     "REQUEST_MORE_EVIDENCE) for one or more claims and write it back "
                     "to ServiceNow."),
     "inputSchema": {"type": "object", "required": ["claim_ids", "decision"], "properties": {
         "claim_ids": {"type": "array", "items": {"type": "string"}},
         "decision": {"type": "string",
                      "enum": ["APPROVE", "REJECT", "REQUEST_MORE_EVIDENCE"]},
         "note": {"type": "string"}}}},
    # ---- app-only (hidden from the model) ----
    {"name": "list_claims", "description": "Queue for the widget.",
     "inputSchema": {"type": "object", "properties": {"route": {"type": "string"}}},
     "_meta": APP_ONLY},
    {"name": "get_job_state", "description": "Poll job progress.",
     "inputSchema": {"type": "object", "required": ["job_id"],
                     "properties": {"job_id": {"type": "string"}}}, "_meta": APP_ONLY},
    {"name": "get_claim", "description": "Cached verdict + evidence image for one claim.",
     "inputSchema": {"type": "object", "required": ["claim_id"],
                     "properties": {"claim_id": {"type": "string"}}}, "_meta": APP_ONLY},
    {"name": "upload_and_analyze", "description": "Analyze an uploaded photo pair.",
     "inputSchema": {"type": "object", "required": ["reference_b64", "return_b64"],
                     "properties": {"reference_b64": {"type": "string"},
                                    "return_b64": {"type": "string"},
                                    "category": {"type": "string"},
                                    "item": {"type": "string"},
                                    "claim_text": {"type": "string"}}},
     "_meta": APP_ONLY},
    {"name": "batch_analyze",
     "description": ("Run batch QC analysis across all pending return claims in parallel "
                     "(for scheduled 9 AM runs or manual batch triggers)."),
     "inputSchema": {"type": "object", "properties": {}}, "_meta": _ui_meta()},
    {"name": "get_stats", "description": "Accuracy/throughput/cost panel.",
     "inputSchema": {"type": "object", "properties": {}}, "_meta": APP_ONLY},
]

# ------------------------- state -------------------------
POLICY = pipeline.Policy()
REUSE = checks.ReuseIndex()
_known = ROOT / "data" / "known_images"
if _known.exists():
  REUSE.add_dir(str(_known))
CLAIMS, EXPECTED = load_claims()
CLAIM_BY_ID = {c.claim_id: c for c in CLAIMS}
VERDICTS: dict[str, Any] = {}       # claim_id -> Verdict
DECISIONS: dict[str, dict] = {}     # claim_id -> analyst decision
JOBS: dict[str, dict] = {}
POOL = cf.ThreadPoolExecutor(max_workers=int(os.environ.get("QC_WORKERS", 8)))
LOCK = threading.Lock()


def _sn():
  if not os.environ.get("SN_INSTANCE"):
    return None
  from qc_core.servicenow import ServiceNow
  return ServiceNow()


def _resolve_claim(ref: str) -> Claim:
  """C01 -> manifest claim. INC... -> ServiceNow ticket + its attachments."""
  if ref in CLAIM_BY_ID:
    return CLAIM_BY_ID[ref]
  sn = _sn()
  if sn and ref.upper().startswith("INC"):
    inc = sn.find_one("incident", f"number={ref.upper()}")
    if not inc:
      raise ValueError(f"Ticket {ref} not found")
    base = next((c for c in CLAIMS if c.order_item_id == inc.get("u_order_item_id")), None)
    ret = []
    for a in sn.list_attachments("incident", inc["sys_id"]):
      if a["file_name"].startswith("evidence_"):
        continue
      p = UPLOAD_DIR / f"{inc['number']}_{a['file_name']}"
      p.write_bytes(sn.download_attachment(a["sys_id"]))
      kind = "video" if (a.get("content_type") or "").startswith("video") else "image"
      ret.append(Media(path=str(p), kind=kind))
    claim = (base.model_copy(deep=True) if base else Claim(
        claim_id=inc["number"], order_item_id=inc.get("u_order_item_id", ""),
        category="Unknown", item=inc.get("short_description", ""),
        claim_reason=inc.get("u_claim_reason", "")))
    claim.claim_id = inc["number"]
    claim.ticket_sys_id = inc["sys_id"]
    claim.ticket_number = inc["number"]
    claim.claim_text = inc.get("description") or claim.claim_text
    if ret:
      claim.return_media = ret
    return claim
  raise ValueError(f"Unknown claim reference: {ref}")


def _ensure_sn_ticket(sn, c: Optional[Claim], cid: str) -> Optional[str]:
  if not sn or not c:
    return None
  if c.ticket_sys_id:
    return c.ticket_sys_id
  try:
    inc = sn.find_one("incident", f"u_order_item_id={c.order_item_id}") if c.order_item_id else None
    if inc and inc.get("sys_id"):
      c.ticket_sys_id = inc["sys_id"]
      c.ticket_number = inc.get("number", "")
      return c.ticket_sys_id
    created = sn.create("incident", {
        "short_description": f"Return claim {cid}: {c.item}",
        "description": c.claim_text or f"Customer return claim for {c.item}",
        "u_order_item_id": c.order_item_id or cid,
        "u_claim_reason": c.claim_reason or "damaged",
    })
    c.ticket_sys_id = created.get("sys_id")
    c.ticket_number = created.get("number", "")
    return c.ticket_sys_id
  except Exception as e:  # pylint: disable=broad-except
    log.warning("ServiceNow lookup/create skipped for %s: %s", cid, e)
    return None


def _start_job(claim: Claim) -> str:
  job_id = uuid.uuid4().hex[:12]
  JOBS[job_id] = {"job_id": job_id, "claim_id": claim.claim_id, "status": "running",
                  "stage": "queued", "started": time.time()}

  def run():
    try:
      gate_fn = compare_fn = None
      if DRY_RUN and claim.claim_id in EXPECTED:
        gate_fn, compare_fn = fake_fns(EXPECTED)
      v = pipeline.analyze(claim, POLICY, REUSE, OUT_DIR, gate_fn, compare_fn,
                           progress=lambda s: JOBS[job_id].update(stage=s))
      with LOCK:
        VERDICTS[claim.claim_id] = v
      sn = _sn()
      sid = _ensure_sn_ticket(sn, claim, claim.claim_id)
      if sn and sid:
        sn.write_verdict(sid, v, v.evidence_image_path)
      JOBS[job_id].update(status="done", stage="done")
    except Exception as e:  # pylint: disable=broad-except
      log.exception("job %s failed", job_id)
      JOBS[job_id].update(status="error", error=str(e))

  POOL.submit(run)
  return job_id


def _encode_media_list(media_list) -> list[dict]:
  items = []
  for m in media_list or []:
    p = pathlib.Path(m.path)
    entry = {"name": p.name, "kind": m.kind}
    if m.kind == "image" and p.exists():
      entry["data_uri"] = "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()
    items.append(entry)
  return items


def _verdict_view(cid: str, with_image: bool = False) -> dict:
  v = VERDICTS.get(cid)
  c = CLAIM_BY_ID.get(cid)
  out = {"claim_id": cid, "item": c.item if c else cid,
         "order_item_id": c.order_item_id if c else cid,
         "claim_reason": c.claim_reason if c else "",
         "claim_text": c.claim_text if c else "",
         "category": c.category if c else "", "value": c.claim_value if c else 0,
         "status": "pending" if v is None else "analyzed",
         "analyst_decision": DECISIONS.get(cid)}
  if with_image and c is not None:
    out["dispatch_media_items"] = _encode_media_list(c.dispatch_media or c.catalog_media)
    out["return_media_items"] = _encode_media_list(c.return_media)
  if v is not None:
    out.update(v.model_dump(mode="json", exclude={"evidence_image_path"}))
    out["analyzed_at"] = time.strftime("%H:%M:%S UTC", time.gmtime())
    if with_image and v.evidence_image_path and pathlib.Path(v.evidence_image_path).exists():
      out["evidence_image"] = "data:image/jpeg;base64," + base64.b64encode(
          pathlib.Path(v.evidence_image_path).read_bytes()).decode()
  return out


# ------------------------- tool impls -------------------------
def t_open_qc_workbench(args):
  focus = args.get("claim_id")
  n_flag = sum(1 for v in VERDICTS.values() if v.route in (Route.FLAGGED, Route.HIGH_VALUE))
  text = (f"Opened the Return QC Workbench. {len(VERDICTS)}/{len(CLAIMS)} claims analyzed, "
          f"{n_flag} awaiting human review.")
  return text, {"view": "queue", "focus": focus}


def t_analyze_claim(args):
  claim = _resolve_claim(args["claim_ref"])
  job = _start_job(claim)
  return (f"Started QC analysis for {claim.claim_id} (job {job}). Progress is shown in the "
          "workbench; the verdict will be written back to the ticket."), {
              "view": "claim", "focus": claim.claim_id, "job_id": job}


def t_apply_decision(args):
  sn = _sn()
  done = []
  for cid in args["claim_ids"]:
    DECISIONS[cid] = {"decision": args["decision"], "note": args.get("note", ""),
                      "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    v = VERDICTS.get(cid)
    c = CLAIM_BY_ID.get(cid)
    sys_id = _ensure_sn_ticket(sn, c, cid)
    if sn and sys_id:
      sn.update("incident", sys_id, {
          "u_qc_decision": args["decision"],
          "work_notes": f"Analyst final decision in Return QC Workbench: {args['decision']}. {args.get('note','')}",
          "state": "6" if args["decision"] in ("APPROVE", "REJECT") else "3"})
    done.append(cid)
  agree = sum(1 for cid in done if VERDICTS.get(cid) and
              VERDICTS[cid].decision.value == args["decision"])
  return f"Recorded {args['decision']} for {len(done)} claim(s) ({agree} matched the AI).", {
      "updated": done}


def t_list_claims(args):
  rows = [_verdict_view(c.claim_id) for c in CLAIMS]
  rows += [_verdict_view(cid) for cid in VERDICTS if cid not in CLAIM_BY_ID]
  if args.get("route"):
    rows = [r for r in rows if r.get("route") == args["route"]]
  return f"{len(rows)} claims", {"claims": rows}


def t_get_job_state(args):
  j = JOBS.get(args["job_id"])
  if not j:
    return "unknown job", {"status": "error", "error": "unknown job"}
  out = dict(j)
  if j["status"] == "done":
    out["verdict"] = _verdict_view(j["claim_id"], with_image=True)
  return j["status"], out


def t_get_claim(args):
  cid = args["claim_id"]
  if cid not in VERDICTS:
    return "not analyzed", {"status": "pending", "claim_id": cid}
  return "ok", {"status": "analyzed", "verdict": _verdict_view(cid, with_image=True)}


def t_upload_and_analyze(args):
  uid = "UP-" + uuid.uuid4().hex[:6].upper()
  ref = UPLOAD_DIR / f"{uid}_reference.jpg"
  ret = UPLOAD_DIR / f"{uid}_return.jpg"
  for p, key in ((ref, "reference_b64"), (ret, "return_b64")):
    data = args[key].split(",", 1)[-1]
    p.write_bytes(base64.b64decode(data))
  claim = Claim(claim_id=uid, order_item_id=uid, category=args.get("category", "Electronics"),
                item=args.get("item", "Uploaded item"), claim_reason="damaged",
                claim_text=args.get("claim_text", ""),
                dispatch_media=[Media(path=str(ref))], return_media=[Media(path=str(ret))])
  CLAIM_BY_ID[uid] = claim
  CLAIMS.insert(0, claim)
  job = _start_job(claim)
  return f"Analyzing uploaded pair as {uid}", {"job_id": job, "claim_id": uid}


def t_batch_analyze(args):
  jobs = [_start_job(c) for c in CLAIMS]
  return f"Started {len(jobs)} jobs", {"job_ids": jobs}


def _daily_9am_scheduler():
  from datetime import datetime
  from zoneinfo import ZoneInfo
  tz = ZoneInfo(os.environ.get("QC_SCHEDULE_TZ", "Asia/Kolkata"))
  last_run_date = None
  while True:
    now = datetime.now(tz)
    if now.hour == 9 and now.minute == 0 and last_run_date != now.date():
      last_run_date = now.date()
      log.info("9:00 AM scheduled batch trigger firing for %d claims", len(CLAIMS))
      t_batch_analyze({})
    time.sleep(20)


threading.Thread(target=_daily_9am_scheduler, daemon=True).start()


def t_get_stats(args):
  vs = [v for cid, v in VERDICTS.items() if cid in EXPECTED]
  n = len(vs)
  correct = sum(1 for v in vs if v.decision.value == EXPECTED[v.claim_id]["decision"])
  auto = sum(1 for v in vs if v.route.value.startswith("AUTO") or v.route == Route.RETAKE)
  cost = sum(v.est_cost_usd for v in VERDICTS.values())
  lat = [v.latency_s for v in VERDICTS.values()]
  overrides = sum(1 for cid, d in DECISIONS.items()
                  if VERDICTS.get(cid) and VERDICTS[cid].decision.value != d["decision"])
  return "stats", {
      "analyzed": len(VERDICTS), "accuracy_vs_expected": (correct / n) if n else None,
      "auto_actioned_pct": (auto / n) if n else None, "analyst_decisions": len(DECISIONS),
      "overrides": overrides, "total_cost_usd": round(cost, 4),
      "cost_per_claim_usd": round(cost / len(VERDICTS), 5) if VERDICTS else None,
      "avg_latency_s": round(sum(lat) / len(lat), 1) if lat else None,
      "phase": POLICY.phase}


IMPL = {"open_qc_workbench": t_open_qc_workbench, "analyze_claim": t_analyze_claim,
        "apply_decision": t_apply_decision, "list_claims": t_list_claims,
        "get_job_state": t_get_job_state, "get_claim": t_get_claim,
        "upload_and_analyze": t_upload_and_analyze,
        "batch_analyze": t_batch_analyze, "get_stats": t_get_stats}
UI_TOOLS = {"open_qc_workbench", "analyze_claim"}


# ------------------------- auth (layer 2) -------------------------
def _verify_user(req: Request) -> Optional[str]:
  """Returns user email if the bearer token was issued for our OAuth client."""
  authz = req.headers.get("authorization", "")
  if not authz.lower().startswith("bearer "):
    return None
  tok = authz.split(" ", 1)[1]
  try:
    r = requests.get("https://oauth2.googleapis.com/tokeninfo",
                     params={"access_token": tok}, timeout=5)
    if r.status_code != 200:
      return None
    info = r.json()
    if OAUTH_CLIENT_ID and OAUTH_CLIENT_ID not in (info.get("aud"), info.get("azp")):
      return None
    return info.get("email", "unknown")
  except requests.RequestException:
    return None


# ------------------------- JSON-RPC -------------------------
app = FastAPI(title="Return QC Workbench MCP")


def _ok(rid, result):
  return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, msg):
  return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle(msg: dict, user: Optional[str]) -> Optional[dict]:
  method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
  if rid is None:  # notification
    return None
  if method == "initialize":
    return _ok(rid, {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                     "capabilities": {"tools": {"listChanged": False},
                                      "resources": {"listChanged": False}},
                     "serverInfo": {"name": "return-qc-workbench", "version": "0.1.0"},
                     "instructions": ("Return QC tools for e-commerce return fraud. Use "
                                      "open_qc_workbench to show the panel; never use canvas "
                                      "for this workflow.")})
  if method == "ping":
    return _ok(rid, {})
  if method == "tools/list":
    return _ok(rid, {"tools": TOOLS})
  if method == "resources/list":
    return _ok(rid, {"resources": [{"uri": UI_URI, "name": "Return QC Workbench",
                                    "mimeType": UI_MIME, "_meta": RESOURCE_META}]})
  if method == "resources/read":
    if params.get("uri") != UI_URI:
      return _err(rid, -32602, "unknown resource")
    return _ok(rid, {"contents": [{"uri": UI_URI, "mimeType": UI_MIME,
                                   "text": WIDGET_HTML, "_meta": RESOURCE_META}]})
  if method == "tools/call":
    name = params.get("name")
    if name not in IMPL:
      return _err(rid, -32602, f"unknown tool {name}")
    if REQUIRE_MCP_AUTH in ("tools", "strict") and not user:
      return _err(rid, 401, "unauthenticated")
    try:
      text, data = IMPL[name](params.get("arguments") or {})
      res = {"content": [{"type": "text", "text": text}], "structuredContent": data}
      if name in UI_TOOLS:
        res["_meta"] = _ui_meta()
      return _ok(rid, res)
    except Exception as e:  # pylint: disable=broad-except
      log.exception("tool %s failed", name)
      return _ok(rid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
  return _err(rid, -32601, f"method not found: {method}")


@app.post("/mcp")
async def mcp(req: Request):
  user = _verify_user(req) if REQUIRE_MCP_AUTH != "off" else None
  if REQUIRE_MCP_AUTH == "strict" and not user:
    return JSONResponse(status_code=401, content=_err(None, 401, "unauthenticated"),
                        headers={"WWW-Authenticate": "Bearer"})
  body = await req.json()
  if isinstance(body, list):
    out = [r for r in (handle(m, user) for m in body) if r is not None]
    return JSONResponse(out) if out else JSONResponse(status_code=202, content=None)
  r = handle(body, user)
  return JSONResponse(r) if r is not None else JSONResponse(status_code=202, content=None)


@app.get("/healthz")
def healthz():
  return {"ok": True, "claims": len(CLAIMS), "analyzed": len(VERDICTS), "dry_run": DRY_RUN}


@app.get("/dev")
def dev():
  """Local preview of the widget outside GE (disabled in production)."""
  from fastapi.responses import HTMLResponse
  if os.environ.get("QC_ENABLE_DEV", "0") != "1":
    return JSONResponse(status_code=404, content={"detail": "disabled"})
  return HTMLResponse(WIDGET_HTML)
