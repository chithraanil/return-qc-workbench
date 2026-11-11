"""End-to-end QC pipeline + routing policy (decision #16)."""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from qc_core import checks, evidence
from qc_core.schemas import (Claim, ComparisonBasis, Decision, LiableParty,
                             Media, ModelComparison, ReasonCode, Route, Verdict)


@dataclass
class Policy:
  """Who has the final say. Tuned per pilot phase."""
  phase: str = os.environ.get("QC_PHASE", "FULL")  # SHADOW | AUTO_APPROVE | FULL
  auto_threshold: float = 0.90
  review_floor: float = 0.60
  high_value: float = float(os.environ.get("QC_HIGH_VALUE", 15000))  # in claim currency
  audit_rate: float = float(os.environ.get("QC_AUDIT_RATE", 0.05))
  seed: Optional[int] = 7
  _rng: random.Random = field(default_factory=random.Random, repr=False)

  def __post_init__(self):
    if self.seed is not None:
      self._rng.seed(self.seed)

  def route(self, claim: Claim, mc: ModelComparison,
            basis: ComparisonBasis) -> Route:
    if claim.claim_value >= self.high_value:
      return Route.HIGH_VALUE
    if self.phase == "SHADOW":
      return Route.FLAGGED
    if mc.confidence < self.auto_threshold or mc.decision == Decision.HUMAN_REVIEW:
      return Route.FLAGGED
    if basis == ComparisonBasis.CATALOG_LISTING:
      return Route.FLAGGED  # weaker evidence is always human-confirmed
    if mc.decision == Decision.APPROVE:
      return Route.AUDIT_SAMPLE if self._rng.random() < self.audit_rate else Route.AUTO_APPROVE
    if mc.decision == Decision.REQUEST_MORE_EVIDENCE:
      return Route.AUTO_REQUEST_EVIDENCE
    if mc.decision == Decision.REJECT:
      # Zero-refund rejections need a human until phase FULL.
      return Route.AUTO_REJECT if self.phase == "FULL" else Route.FLAGGED
    return Route.FLAGGED


def choose_basis(claim: Claim) -> tuple[ComparisonBasis, list[Media]]:
  videos = [m for m in claim.dispatch_media if m.kind == "video"]
  photos = [m for m in claim.dispatch_media if m.kind == "image"]
  if photos or videos:
    basis = ComparisonBasis.PACKING_VIDEO if videos and not photos else ComparisonBasis.DISPATCH_PHOTO
    return basis, claim.dispatch_media
  if claim.catalog_media or claim.catalog_attributes:
    return ComparisonBasis.CATALOG_LISTING, claim.catalog_media
  return ComparisonBasis.NONE, []


def _first_image(media: list[Media]):
  for m in media:
    if m.kind == "image" and not m.path.startswith("gs://"):
      return checks.load_image(m.path)
  return None


Comparer = Callable[[Claim, str, list[Media], str], tuple[ModelComparison, float]]


def analyze(claim: Claim,
            policy: Policy,
            reuse_index: Optional[checks.ReuseIndex] = None,
            out_dir: str = "out",
            gate_fn=None,
            compare_fn: Optional[Comparer] = None,
            progress: Callable[[str], None] = lambda s: None) -> Verdict:
  """Runs gate -> basis -> checks -> deep compare -> policy -> evidence image."""
  from qc_core import gemini  # lazy: lets dry-runs work without credentials
  gate_fn = gate_fn or gemini.quality_gate
  compare_fn = compare_fn or gemini.compare
  t0 = time.time()
  cost = 0.0
  models = []

  progress("quality_gate")
  gate, c = gate_fn(claim)
  cost += c
  models.append(gemini.FLASH_MODEL)
  if not gate.usable:
    v = Verdict(claim_id=claim.claim_id, decision=Decision.REQUEST_MORE_EVIDENCE,
                route=Route.RETAKE, confidence=0.95,
                reason_codes=[ReasonCode.INSUFFICIENT_EVIDENCE],
                comparison_basis=ComparisonBasis.NONE, liable_party=LiableParty.NONE,
                summary="Customer media not usable: " + "; ".join(gate.issues),
                quality_gate=gate,
                customer_message_draft=("Thanks for your request. To process it, please "
                                        "upload a clear photo: " + "; ".join(gate.issues)),
                models_used=models, latency_s=time.time() - t0, est_cost_usd=cost)
    return v

  progress("matching_reference")
  basis, reference = choose_basis(claim)

  progress("deterministic_checks")
  ret_imgs = [checks.load_image(m.path) for m in claim.return_media
              if m.kind == "image" and not m.path.startswith("gs://")]
  signals = checks.run_checks(ret_imgs, claim.delivered_at, reuse_index)

  progress("deep_comparison")
  mc, c = compare_fn(claim, basis.value, reference, signals.model_dump_json())
  cost += c
  models.append(gemini.DEEP_MODEL)

  # Deterministic signals can only make the outcome MORE cautious.
  if (signals.phash_reuse or signals.exif_anomaly) and \
      ReasonCode.IMAGE_MANIPULATION_SUSPECTED not in mc.reason_codes:
    mc.reason_codes.append(ReasonCode.IMAGE_MANIPULATION_SUSPECTED)
    if mc.decision == Decision.APPROVE:
      mc.decision = Decision.REQUEST_MORE_EVIDENCE

  route = policy.route(claim, mc, basis)
  v = Verdict(claim_id=claim.claim_id, decision=mc.decision, route=route,
              confidence=mc.confidence, reason_codes=mc.reason_codes,
              comparison_basis=basis, liable_party=mc.liable_party,
              summary=mc.summary, evidence=mc.evidence,
              attribute_checks=mc.attribute_checks,
              video_keyframes=mc.video_keyframes, deterministic_signals=signals,
              quality_gate=gate, customer_message_draft=mc.customer_message_draft,
              seller_message_draft=mc.seller_message_draft, models_used=models)

  progress("rendering_evidence")
  ret_img = ret_imgs[0] if ret_imgs else None
  if ret_img is not None:
    label = {"CATALOG_LISTING": "CATALOG LISTING", "PACKING_VIDEO": "PACKING VIDEO"
             }.get(basis.value, "DISPATCH (seller)")
    v.evidence_image_path = evidence.render(
        v, _first_image(reference), ret_img,
        os.path.join(out_dir, f"evidence_{claim.claim_id}.jpg"), label)

  v.latency_s = round(time.time() - t0, 2)
  v.est_cost_usd = round(cost, 5)
  progress("done")
  return v


def verdict_json(v: Verdict) -> str:
  return json.dumps(v.model_dump(mode="json"), indent=2)
