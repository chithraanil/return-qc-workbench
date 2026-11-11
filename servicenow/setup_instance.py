"""Configures a ServiceNow developer instance for the QC demo and seeds claims.

  export SN_INSTANCE=https://devXXXXXX.service-now.com SN_USER=admin
  read -s SN_PASSWORD && export SN_PASSWORD        # never paste into chat
  python servicenow/setup_instance.py configure --analyst-email you@altostrat.com
  python servicenow/setup_instance.py seed          # creates 19 claim tickets + photos
  python servicenow/setup_instance.py seed --only C01,C13

Idempotent: re-running skips anything that already exists.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.run import load_claims  # noqa: E402
from qc_core.servicenow import ServiceNow  # noqa: E402

FIELDS = [  # (column, label, type, max_length, choices)
    ("u_order_item_id", "Order Item ID", "string", 40, None),
    ("u_buyer_id", "Buyer ID", "string", 40, None),
    ("u_seller_id", "Seller ID", "string", 40, None),
    ("u_claim_reason", "Claim Reason", "string", 40,
     ["damaged", "wrong_item", "used", "missing_part"]),
    ("u_qc_decision", "QC Decision", "string", 40,
     ["APPROVE", "REJECT", "REQUEST_MORE_EVIDENCE", "HUMAN_REVIEW"]),
    ("u_qc_confidence", "QC Confidence", "decimal", None, None),
    ("u_qc_reason_codes", "QC Reason Codes", "string", 255, None),
    ("u_liable_party", "Liable Party", "string", 20,
     ["CUSTOMER", "SELLER", "CARRIER", "NONE"]),
    ("u_claim_value", "Claim Value", "decimal", None, None),
]

PRODUCER_SCRIPT = """
current.category = 'return_claim';
current.short_description = 'Return claim: ' + producer.claim_reason + ' (' + producer.order_item_id + ')';
current.description = producer.description;
current.u_order_item_id = producer.order_item_id;
current.u_claim_reason = producer.claim_reason;
current.u_buyer_id = 'B-5521';
current.u_seller_id = 'S-117';
current.assigned_to.setDisplayValue('qc.analyst');
"""


def ensure(sn: ServiceNow, table: str, query: str, body: dict, label: str) -> dict:
  found = sn.find_one(table, query)
  if found:
    print(f"  = exists: {label}")
    return found
  rec = sn.create(table, body)
  print(f"  + created: {label}")
  return rec


def configure(sn: ServiceNow, analyst_email: str):
  print("1) Category 'Return Claim'")
  ensure(sn, "sys_choice", "name=incident^element=category^value=return_claim",
         {"name": "incident", "element": "category", "value": "return_claim",
          "label": "Return Claim", "language": "en"}, "category choice")

  print("2) Custom fields on incident")
  for col, label, typ, maxlen, choices in FIELDS:
    body = {"name": "incident", "element": col, "column_label": label,
            "internal_type": typ, "active": "true"}
    if maxlen:
      body["max_length"] = str(maxlen)
    if choices:
      body["choice"] = "1"  # dropdown with --None--
    ensure(sn, "sys_dictionary", f"name=incident^element={col}", body, col)
    for ch in choices or []:
      ensure(sn, "sys_choice", f"name=incident^element={col}^value={ch}",
             {"name": "incident", "element": col, "value": ch, "label": ch,
              "language": "en"}, f"{col}={ch}")

  print("3) Users")
  itil = sn.find_one("sys_user_role", "name=itil")
  users = [("qc.analyst", "QC", "Analyst", analyst_email, False),
           ("qc.agent.integration", "QC", "Agent Integration",
            "qc.agent@example.invalid", True),
           ("demo.customer", "Demo", "Customer", "demo.customer@example.invalid", False)]
  for uname, first, last, email, ws_only in users:
    u = ensure(sn, "sys_user", f"user_name={uname}",
               {"user_name": uname, "first_name": first, "last_name": last,
                "email": email, "active": "true",
                "web_service_access_only": str(ws_only).lower()}, uname)
    if uname != "demo.customer" and itil:
      ensure(sn, "sys_user_has_role", f"user={u['sys_id']}^role={itil['sys_id']}",
             {"user": u["sys_id"], "role": itil["sys_id"]}, f"{uname} -> itil")
  print("   NOTE: set passwords in the UI (User record > Set Password). "
        "Store qc.agent.integration's password in Secret Manager only.")

  print("4) 'Request a Refund' record producer (Service Portal)")
  cat = sn.find_one("sc_catalog", "title=Service Catalog")
  prod = ensure(sn, "sc_cat_item_producer", "name=Request a Refund",
                {"name": "Request a Refund", "table_name": "incident",
                 "short_description": "Report a damaged, wrong or incomplete order",
                 "script": PRODUCER_SCRIPT, "active": "true",
                 "sc_catalogs": cat["sys_id"] if cat else ""}, "record producer")
  variables = [("order_item_id", "Order Item ID", "6", True),   # 6 = single line text
               ("claim_reason", "Reason", "5", True),           # 5 = select box
               ("description", "Describe the issue", "2", False)]  # 2 = multi-line
  for order, (name, q, vtype, mand) in enumerate(variables, start=1):
    v = ensure(sn, "item_option_new", f"cat_item={prod['sys_id']}^name={name}",
               {"cat_item": prod["sys_id"], "name": name, "question_text": q,
                "type": vtype, "mandatory": str(mand).lower(), "order": str(order * 100)},
               f"variable {name}")
    if name == "claim_reason":
      for i, ch in enumerate(["damaged", "wrong_item", "used", "missing_part"]):
        ensure(sn, "question_choice", f"question={v['sys_id']}^value={ch}",
               {"question": v["sys_id"], "value": ch, "text": ch, "order": str(i)},
               f"choice {ch}")
  print(f"\nDone. Portal: {sn.base}/sp  (search 'Request a Refund')")


def seed(sn: ServiceNow, only: set[str]):
  claims, _ = load_claims()
  analyst = sn.find_one("sys_user", "user_name=qc.analyst")
  for c in claims:
    if only and c.claim_id not in only:
      continue
    existing = sn.find_one("incident", f"u_order_item_id={c.order_item_id}")
    if existing:
      print(f"  = {c.claim_id}: {existing['number']} exists")
      continue
    inc = sn.create("incident", {
        "category": "return_claim",
        "short_description": f"Return claim: {c.claim_reason} ({c.order_item_id}) - {c.item}",
        "description": c.claim_text,
        "caller_id": "demo.customer",
        "assigned_to": analyst["sys_id"] if analyst else "",
        "u_order_item_id": c.order_item_id, "u_claim_reason": c.claim_reason,
        "u_buyer_id": "B-5521", "u_seller_id": "S-117",
        "u_claim_value": c.claim_value,
    })
    n = 0
    for m in c.return_media:  # customer evidence lives on the ticket
      if pathlib.Path(m.path).exists():
        sn.upload_attachment("incident", inc["sys_id"], m.path)
        n += 1
    print(f"  + {c.claim_id}: {inc['number']} ({n} attachments)")


def main():
  ap = argparse.ArgumentParser()
  sub = ap.add_subparsers(dest="cmd", required=True)
  c = sub.add_parser("configure")
  c.add_argument("--analyst-email", required=True)
  s = sub.add_parser("seed")
  s.add_argument("--only", default="")
  args = ap.parse_args()
  sn = ServiceNow()
  if args.cmd == "configure":
    configure(sn, args.analyst_email)
  else:
    seed(sn, set(filter(None, args.only.split(","))))


if __name__ == "__main__":
  main()
