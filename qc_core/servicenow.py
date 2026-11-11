"""Minimal ServiceNow REST client (Table + Attachment APIs).

Credentials come from env / Secret Manager, never from code:
  SN_INSTANCE (e.g. https://dev123456.service-now.com), SN_USER, SN_PASSWORD
"""

from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import urllib.parse
from typing import Optional

import requests


class ServiceNow:

  def __init__(self, instance: Optional[str] = None, user: Optional[str] = None,
               password: Optional[str] = None, timeout: int = 30):
    self.base = (instance or os.environ["SN_INSTANCE"]).rstrip("/")
    self.auth = (user or os.environ["SN_USER"], password or os.environ["SN_PASSWORD"])
    self.timeout = timeout
    self.s = requests.Session()
    self.s.auth = self.auth
    self.s.headers.update({"Accept": "application/json"})

  # ---------- Table API ----------
  def get(self, table: str, query: str = "", fields: str = "", limit: int = 100):
    params = {"sysparm_query": query, "sysparm_limit": limit,
              "sysparm_display_value": "false"}
    if fields:
      params["sysparm_fields"] = fields
    r = self.s.get(f"{self.base}/api/now/table/{table}", params=params,
                   timeout=self.timeout)
    r.raise_for_status()
    return r.json()["result"]

  def create(self, table: str, body: dict) -> dict:
    r = self.s.post(f"{self.base}/api/now/table/{table}", json=body,
                    timeout=self.timeout)
    r.raise_for_status()
    return r.json()["result"]

  def update(self, table: str, sys_id: str, body: dict) -> dict:
    r = self.s.patch(f"{self.base}/api/now/table/{table}/{sys_id}", json=body,
                     timeout=self.timeout)
    r.raise_for_status()
    return r.json()["result"]

  def find_one(self, table: str, query: str) -> Optional[dict]:
    res = self.get(table, query, limit=1)
    return res[0] if res else None

  # ---------- Attachment API ----------
  def list_attachments(self, table: str, sys_id: str) -> list[dict]:
    return self.get("sys_attachment",
                    f"table_name={table}^table_sys_id={sys_id}",
                    fields="sys_id,file_name,content_type,size_bytes")

  def download_attachment(self, att_sys_id: str) -> bytes:
    r = self.s.get(f"{self.base}/api/now/attachment/{att_sys_id}/file",
                   timeout=self.timeout * 2)
    r.raise_for_status()
    return r.content

  def upload_attachment(self, table: str, sys_id: str, path: str,
                        file_name: Optional[str] = None) -> dict:
    name = file_name or pathlib.Path(path).name
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    q = urllib.parse.urlencode({"table_name": table, "table_sys_id": sys_id,
                                "file_name": name})
    r = self.s.post(f"{self.base}/api/now/attachment/file?{q}",
                    data=pathlib.Path(path).read_bytes(),
                    headers={"Content-Type": mime}, timeout=self.timeout * 2)
    r.raise_for_status()
    return r.json()["result"]

  # ---------- QC helpers ----------
  def write_verdict(self, incident_sys_id: str, verdict, evidence_path: Optional[str]):
    """Writes QC fields + work note + evidence image to the incident."""
    codes = ",".join(c.value for c in verdict.reason_codes)
    lines = [f"QC Triage · Decision: {verdict.decision.value} · "
             f"Confidence {verdict.confidence:.2f} · Route: {verdict.route.value}",
             f"Reason codes: {codes or 'none'} · Basis: {verdict.comparison_basis.value}"
             f" · Liable: {verdict.liable_party.value}",
             verdict.summary]
    for a in verdict.attribute_checks:
      lines.append(f"  {'✔' if a.match else '✖'} {a.attribute}: expected "
                   f"'{a.expected}', observed '{a.observed}' {a.note}")
    if verdict.deterministic_signals.phash_reuse:
      lines.append(f"  Reused image match: {verdict.deterministic_signals.phash_match_ref}")
    lines += [f"  {n}" for n in verdict.deterministic_signals.exif_notes
              if "No EXIF" not in n]
    if verdict.customer_message_draft:
      lines.append("Draft to customer: " + verdict.customer_message_draft)
    self.update("incident", incident_sys_id, {
        "u_qc_decision": verdict.decision.value,
        "u_qc_confidence": round(verdict.confidence, 3),
        "u_qc_reason_codes": codes[:255],
        "u_liable_party": verdict.liable_party.value,
        "work_notes": "\n".join(lines),
    })
    if evidence_path and pathlib.Path(evidence_path).exists():
      self.upload_attachment("incident", incident_sys_id, evidence_path)


def to_json(obj) -> str:
  return json.dumps(obj, indent=2, default=str)
