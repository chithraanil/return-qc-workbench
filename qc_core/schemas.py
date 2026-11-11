"""Data contracts for the QC return-fraud engine.

The Verdict model is the single output contract shared by the GE workflow,
the MCP Workbench and ServiceNow write-back.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ReasonCode(str, Enum):
  ITEM_MISMATCH = "ITEM_MISMATCH"
  SERIAL_OR_TAG_MISMATCH = "SERIAL_OR_TAG_MISMATCH"
  USED_OR_WORN = "USED_OR_WORN"
  PRE_EXISTING_DAMAGE_CLAIM = "PRE_EXISTING_DAMAGE_CLAIM"
  TRANSIT_DAMAGE_LIKELY = "TRANSIT_DAMAGE_LIKELY"
  MISSING_PARTS_OR_ACCESSORIES = "MISSING_PARTS_OR_ACCESSORIES"
  IMAGE_MANIPULATION_SUSPECTED = "IMAGE_MANIPULATION_SUSPECTED"
  INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class Decision(str, Enum):
  APPROVE = "APPROVE"
  REJECT = "REJECT"
  REQUEST_MORE_EVIDENCE = "REQUEST_MORE_EVIDENCE"
  HUMAN_REVIEW = "HUMAN_REVIEW"


class LiableParty(str, Enum):
  CUSTOMER = "CUSTOMER"
  SELLER = "SELLER"
  CARRIER = "CARRIER"
  NONE = "NONE"


class ComparisonBasis(str, Enum):
  DISPATCH_PHOTO = "DISPATCH_PHOTO"
  PACKING_VIDEO = "PACKING_VIDEO"
  CATALOG_LISTING = "CATALOG_LISTING"
  NONE = "NONE"


class Route(str, Enum):
  """Where the claim goes after the verdict (decided by policy, not the model)."""
  AUTO_APPROVE = "AUTO_APPROVE"
  AUTO_REQUEST_EVIDENCE = "AUTO_REQUEST_EVIDENCE"
  AUTO_REJECT = "AUTO_REJECT"
  FLAGGED = "FLAGGED"
  HIGH_VALUE = "HIGH_VALUE"
  AUDIT_SAMPLE = "AUDIT_SAMPLE"
  RETAKE = "RETAKE"


class EvidenceItem(BaseModel):
  code: ReasonCode
  explanation: str
  # Normalized [ymin, xmin, ymax, xmax] in 0..1000 (Gemini box convention).
  dispatch_box: Optional[list[int]] = None
  return_box: Optional[list[int]] = None
  dispatch_image_index: int = 0
  return_image_index: int = 0


class VideoKeyframe(BaseModel):
  t: str = Field(description="Timestamp mm:ss in the video")
  observation: str


class AttributeCheck(BaseModel):
  """One row of the attribute-by-attribute match table."""
  attribute: str = Field(description="e.g. color, size, model, imei, brand_tag, seal")
  expected: str = Field(description="Value from ordered item / reference media")
  observed: str = Field(description="Value seen in the customer's return media")
  match: bool
  note: str = ""


class DeterministicSignals(BaseModel):
  phash_reuse: bool = False
  phash_match_ref: Optional[str] = None
  exif_anomaly: bool = False
  exif_notes: list[str] = Field(default_factory=list)


class QualityGate(BaseModel):
  """Output of the cheap Flash gate."""
  usable: bool
  product_visible: bool
  identifier_readable: bool
  issues: list[str] = Field(default_factory=list)


class ModelComparison(BaseModel):
  """What we ask Argon to return (schema-constrained)."""
  decision: Decision
  confidence: float = Field(ge=0.0, le=1.0)
  reason_codes: list[ReasonCode] = Field(default_factory=list)
  evidence: list[EvidenceItem] = Field(default_factory=list)
  attribute_checks: list[AttributeCheck] = Field(default_factory=list)
  video_keyframes: list[VideoKeyframe] = Field(default_factory=list)
  same_item: bool
  liable_party: LiableParty
  summary: str
  customer_message_draft: str
  seller_message_draft: str


class Verdict(BaseModel):
  """Final, policy-applied verdict for one claim."""
  claim_id: str
  decision: Decision
  route: Route
  confidence: float
  reason_codes: list[ReasonCode]
  comparison_basis: ComparisonBasis
  liable_party: LiableParty
  summary: str
  evidence: list[EvidenceItem] = Field(default_factory=list)
  attribute_checks: list[AttributeCheck] = Field(default_factory=list)
  video_keyframes: list[VideoKeyframe] = Field(default_factory=list)
  deterministic_signals: DeterministicSignals = Field(
      default_factory=DeterministicSignals)
  quality_gate: Optional[QualityGate] = None
  customer_message_draft: str = ""
  seller_message_draft: str = ""
  evidence_image_path: Optional[str] = None
  models_used: list[str] = Field(default_factory=list)
  latency_s: float = 0.0
  est_cost_usd: float = 0.0


class Media(BaseModel):
  path: str  # local path or gs:// URI
  kind: str = "image"  # image | video
  mime_type: Optional[str] = None


class Claim(BaseModel):
  claim_id: str
  order_item_id: str
  category: str
  item: str
  claim_reason: str
  claim_text: str = ""
  claim_value: float = 0.0
  currency: str = "INR"
  delivered_at: Optional[str] = None  # ISO date, for EXIF checks
  dispatch_media: list[Media] = Field(default_factory=list)
  return_media: list[Media] = Field(default_factory=list)
  catalog_media: list[Media] = Field(default_factory=list)
  catalog_attributes: dict[str, str] = Field(default_factory=dict)
  ordered_attributes: dict[str, str] = Field(
      default_factory=dict, description="What the customer ordered (SKU attrs)")
  ticket_sys_id: Optional[str] = None  # ServiceNow incident sys_id, if any
  ticket_number: Optional[str] = None
  buyer_id: Optional[str] = None
  seller_id: Optional[str] = None
