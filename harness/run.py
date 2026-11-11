"""Batch harness: runs all claims, prints a scorecard + scale metrics.

  python -m harness.run --dry-run   # plumbing test, no model calls (fake verdicts)
  python -m harness.run             # LIVE: Gemini 3.8 Flash + Gemini 4 Argon
  python -m harness.run --only C01,C13 --phase SHADOW
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import os
import pathlib
import sys
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qc_core import checks, pipeline  # noqa: E402
from qc_core.schemas import (Claim, Decision, LiableParty, Media,  # noqa: E402
                             ModelComparison, QualityGate, ReasonCode)

MEDIA = ROOT / "data" / "media"
KNOWN = ROOT / "data" / "known_images"


def _media(names, kind_hint=None):
  out = []
  for n in names or []:
    kind = "video" if n.lower().endswith((".mp4", ".mov", ".webm")) else "image"
    out.append(Media(path=str(MEDIA / n), kind=kind))
  return out


def load_claims(path=ROOT / "data" / "claims.yaml"):
  doc = yaml.safe_load(pathlib.Path(path).read_text())
  d = doc.get("defaults", {})
  claims, expected = [], {}
  for c in doc["claims"]:
    claims.append(Claim(
        claim_id=c["id"], order_item_id=c["order_item_id"], category=c["category"],
        item=c["item"], claim_reason=c["reason"], claim_text=c.get("text", ""),
        claim_value=c.get("value", 0), currency=d.get("currency", "INR"),
        delivered_at=c.get("delivered_at", d.get("delivered_at")),
        dispatch_media=_media(c.get("dispatch")), return_media=_media(c.get("return")),
        catalog_media=_media(c.get("catalog")),
        catalog_attributes=c.get("catalog_attributes", {}),
        ordered_attributes=c.get("ordered", {})))
    expected[c["id"]] = c["expected"]
  return claims, expected


def missing_media(claim: Claim, allow_video_missing: bool):
  miss = []
  for m in claim.dispatch_media + claim.return_media + claim.catalog_media:
    if not pathlib.Path(m.path).exists():
      if allow_video_missing and m.kind == "video":
        continue
      miss.append(pathlib.Path(m.path).name)
  return miss


def fake_fns(expected):
  """Dry-run stand-ins that echo ground truth: tests plumbing, NOT the model."""
  def gate(claim):
    exp = expected[claim.claim_id]
    ok = exp.get("route") != "RETAKE"
    return QualityGate(usable=ok, product_visible=ok, identifier_readable=ok,
                       issues=[] if ok else ["Photo is blurry; show the serial label clearly"]), 0.0

  def compare(claim, basis, reference, signals_json):
    exp = expected[claim.claim_id]
    dec = Decision(exp["decision"])
    conf = 0.75 if dec == Decision.HUMAN_REVIEW else 0.93
    return ModelComparison(
        decision=dec, confidence=conf,
        reason_codes=[ReasonCode(c) for c in exp.get("codes", [])],
        evidence=[], same_item=True,
        liable_party=LiableParty(exp.get("liable", "NONE")),
        summary="[DRY RUN] echo of expected outcome",
        customer_message_draft="[DRY RUN]", seller_message_draft="[DRY RUN]"), 0.0
  return gate, compare


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--dry-run", action="store_true")
  ap.add_argument("--only", default="")
  ap.add_argument("--phase", default=os.environ.get("QC_PHASE", "FULL"))
  ap.add_argument("--workers", type=int, default=6)
  ap.add_argument("--out", default=str(ROOT / "out"))
  args = ap.parse_args()

  claims, expected = load_claims()
  if args.only:
    keep = set(args.only.split(","))
    claims = [c for c in claims if c.claim_id in keep]

  reuse = checks.ReuseIndex()
  if KNOWN.exists():
    reuse.add_dir(str(KNOWN))

  runnable, skipped = [], {}
  for c in claims:
    miss = missing_media(c, allow_video_missing=args.dry_run)
    (skipped.__setitem__(c.claim_id, miss) if miss else runnable.append(c))

  policy = pipeline.Policy(phase=args.phase)
  gate_fn = compare_fn = None
  if args.dry_run:
    gate_fn, compare_fn = fake_fns(expected)

  t0 = time.time()
  results = {}
  with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
    futs = {ex.submit(pipeline.analyze, c, policy, reuse, args.out, gate_fn, compare_fn): c
            for c in runnable}
    for f in cf.as_completed(futs):
      c = futs[f]
      try:
        results[c.claim_id] = f.result()
      except Exception as e:  # pylint: disable=broad-except
        results[c.claim_id] = e
  wall = time.time() - t0

  # ---- scorecard ----
  print(f"\n{'ID':4} {'EXPECTED':22} {'ACTUAL':22} {'ROUTE':22} {'CONF':>5} {'CODES OK':8} {'SECS':>5}")
  correct = code_ok_n = 0
  routes = collections.Counter()
  cost = 0.0
  for c in claims:
    cid = c.claim_id
    exp = expected[cid]
    if cid in skipped:
      print(f"{cid:4} {exp['decision']:22} {'SKIPPED (missing media)':22} {', '.join(skipped[cid])}")
      continue
    v = results[cid]
    if isinstance(v, Exception):
      print(f"{cid:4} {exp['decision']:22} ERROR: {v}")
      continue
    ok = v.decision.value == exp["decision"]
    exp_codes = set(exp.get("codes", []))
    got = {r.value for r in v.reason_codes}
    codes_ok = exp_codes.issubset(got) if exp_codes else not (got - {"IMAGE_MANIPULATION_SUSPECTED"}) or ok
    correct += ok
    code_ok_n += bool(codes_ok)
    routes[v.route.value] += 1
    cost += v.est_cost_usd
    print(f"{cid:4} {exp['decision']:22} {v.decision.value + (' ✓' if ok else ' ✗'):22} "
          f"{v.route.value:22} {v.confidence:5.2f} {('yes' if codes_ok else 'NO'):8} {v.latency_s:5.1f}")
    pathlib.Path(args.out).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(args.out) / f"verdict_{cid}.json").write_text(pipeline.verdict_json(v))

  n = sum(1 for v in results.values() if not isinstance(v, Exception))
  auto = sum(routes[r] for r in ("AUTO_APPROVE", "AUTO_REJECT", "AUTO_REQUEST_EVIDENCE", "RETAKE"))
  print("\n==== SUMMARY", "(DRY RUN: fake model outputs)" if args.dry_run else "(LIVE)", "====")
  print(f"Claims run: {n}/{len(claims)}  skipped: {len(skipped)}  phase: {args.phase}")
  if n:
    print(f"Decision accuracy: {correct}/{n} = {correct/n:.0%}   reason codes OK: {code_ok_n}/{n}")
    print(f"Auto-actioned: {auto}/{n} = {auto/n:.0%}   routes: {dict(routes)}")
    print(f"Wall time: {wall:.1f}s ({args.workers} parallel)  est. cost: ${cost:.4f} total, ${cost/n:.4f}/claim")
    print(f"Projection @ 50,000 claims/month: ${cost/n*50000:,.0f}/month (model cost only)")
  print(f"Artifacts: {args.out}/verdict_*.json, evidence_*.jpg")


if __name__ == "__main__":
  main()
