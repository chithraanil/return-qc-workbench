"""Gemini calls: Flash quality gate + Argon deep comparison (Vertex / Agent Platform)."""

from __future__ import annotations

import mimetypes
import os
import pathlib
from typing import Optional, Sequence

from google import genai
from google.genai import types

from qc_core.schemas import Claim, Media, ModelComparison, QualityGate

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "agent-builder-ge")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
FLASH_MODEL = os.environ.get("QC_FLASH_MODEL", "gemini-3.8-flash")
DEEP_MODEL = os.environ.get("QC_DEEP_MODEL", "gemini-4-argon")

# USD per 1M tokens (input, output). Argon = introductory list price per the
# Gemini 4 sales FAQ; Flash is a PLACEHOLDER: confirm on the pricing page.
PRICES = {
    DEEP_MODEL: (float(os.environ.get("QC_DEEP_IN", 2.0)),
                 float(os.environ.get("QC_DEEP_OUT", 10.0))),
    FLASH_MODEL: (float(os.environ.get("QC_FLASH_IN", 0.30)),
                  float(os.environ.get("QC_FLASH_OUT", 2.50))),
}

_client: Optional[genai.Client] = None


def client() -> genai.Client:
  global _client
  if _client is None:
    _client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
  return _client


def _part(m: Media) -> types.Part:
  mime = m.mime_type or mimetypes.guess_type(m.path)[0] or (
      "video/mp4" if m.kind == "video" else "image/jpeg")
  if m.path.startswith("gs://"):
    return types.Part.from_uri(file_uri=m.path, mime_type=mime)
  return types.Part.from_bytes(data=pathlib.Path(m.path).read_bytes(),
                               mime_type=mime)


def _labelled(label: str, media: Sequence[Media]) -> list:
  parts: list = []
  for i, m in enumerate(media):
    parts.append(f"[{label} #{i} ({m.kind})]")
    parts.append(_part(m))
  return parts


def _cost(model: str, resp) -> float:
  u = getattr(resp, "usage_metadata", None)
  if not u:
    return 0.0
  pin, pout = PRICES.get(model, (0.0, 0.0))
  tin = u.prompt_token_count or 0
  tout = (u.candidates_token_count or 0) + (
      getattr(u, "thoughts_token_count", 0) or 0)
  return (tin * pin + tout * pout) / 1e6


GATE_PROMPT = """You are a photo-quality gate for e-commerce return claims.
Judge ONLY whether the customer's media is legible enough to proceed to comparison.
Claimed item: {item} ({category}). Claim reason: {reason}.
- usable: true unless the image itself is severely blurry/out-of-focus, completely black/dark,
  or has zero legible visual or text content. Staged demo or labeled product cards ARE usable
  (usable: true) as long as they are sharp and readable; only mark usable: false when the image
  is deliberately blurred or obscured (such as a blurry out-of-focus photo).
- product_visible: the claimed product or its labeled representation is clearly visible.
- identifier_readable: a serial/IMEI/barcode/brand/size tag is legible (false if none shown).
- issues: short, customer-friendly retake instructions if not usable.
"""


def quality_gate(claim: Claim) -> tuple[QualityGate, float]:
  contents = _labelled("RETURN", claim.return_media) + [
      GATE_PROMPT.format(item=claim.item, category=claim.category,
                         reason=claim.claim_reason)]
  resp = client().models.generate_content(
      model=FLASH_MODEL,
      contents=contents,
      config=types.GenerateContentConfig(
          response_mime_type="application/json",
          response_schema=QualityGate,
          temperature=0.0,
      ),
  )
  return resp.parsed, _cost(FLASH_MODEL, resp)


CATEGORY_CHECKLISTS = {
    "Electronics": ["model", "color", "storage_or_variant", "serial_or_imei",
                    "screen_condition", "accessories_present", "packaging_condition"],
    "Fashion": ["color", "size_label", "brand_tag_attached", "material_or_pattern",
                "stitching_and_hardware", "signs_of_wear", "stains_or_damage"],
    "Home": ["model_or_design", "color", "pieces_count", "breakage_or_chips",
             "accessories_present", "packaging_condition"],
    "Beauty": ["product_variant", "seal_intact", "fill_level", "batch_code",
               "container_damage"],
    "Accessories": ["model_or_design", "color", "brand_markings", "scratches_or_wear",
                    "case_and_accessories"],
}

COMPARE_PROMPT = """You are a senior Quality Control investigator for a multi-seller
e-commerce marketplace. Decide whether a customer's return/refund claim is valid by
comparing the REFERENCE evidence with the customer's RETURN evidence.

Claim: item="{item}" category="{category}" reason="{reason}"
Customer statement: "{claim_text}"
Ordered item attributes (what the customer bought): {ordered_attrs}
Reference basis: {basis}
{catalog_attrs}
Deterministic signals (from code, trust these): {signals}

Attribute checklist for this category (fill attribute_checks with one row per
attribute you can assess; expected = ordered/reference value, observed = return value):
{checklist}

Investigate, in order:
1. Same item? Compare model, colour, variant, logos, stitching/hardware, serial/IMEI/
   barcode/brand tags. A different serial or counterfeit details => not the same item.
2. Seller error? If the REFERENCE itself differs from the ORDERED attributes (wrong
   size/colour/variant dispatched), the claim is valid and the SELLER is liable.
3. Condition change: new wear, stains, removed tags, broken seals, missing parts.
   For packing videos, cite the timestamp where each component is shown/packed.
4. Damage attribution: if damage is visible in the REFERENCE too => PRE_EXISTING
   (seller liable). If damage matches crushed/torn packaging => TRANSIT (carrier).
5. Manipulation: inconsistent lighting/edges, pasted cracks, stock-photo look,
   or deterministic reuse/EXIF flags.

Rules:
- Use ONLY these reason codes: ITEM_MISMATCH, SERIAL_OR_TAG_MISMATCH, USED_OR_WORN,
  PRE_EXISTING_DAMAGE_CLAIM, TRANSIT_DAMAGE_LIKELY, MISSING_PARTS_OR_ACCESSORIES,
  IMAGE_MANIPULATION_SUSPECTED, INSUFFICIENT_EVIDENCE.
- decision: APPROVE (claim valid), REJECT (claim contradicted by evidence),
  REQUEST_MORE_EVIDENCE (e.g. suspected manipulation -> ask for live unboxing video),
  HUMAN_REVIEW (genuinely ambiguous or no visible issue).
- confidence: calibrated probability that your decision is correct. Be conservative
  when the basis is CATALOG_LISTING or images are poor.
- evidence: one item per finding, with boxes [ymin,xmin,ymax,xmax] normalized 0-1000 on
  the referenced image index. Omit a box if not localizable.
- Never accuse; messages must be polite, factual and cite what was observed.
"""


def compare(claim: Claim, basis: str, reference: Sequence[Media],
            signals_json: str) -> tuple[ModelComparison, float]:
  attrs = ""
  if basis == "CATALOG_LISTING" and claim.catalog_attributes:
    attrs = "Catalog attributes: " + ", ".join(
        f"{k}={v}" for k, v in claim.catalog_attributes.items())
  contents = (_labelled("REFERENCE", reference) +
              _labelled("RETURN", claim.return_media) + [
                  COMPARE_PROMPT.format(
                      item=claim.item, category=claim.category,
                      reason=claim.claim_reason, claim_text=claim.claim_text,
                      ordered_attrs=claim.ordered_attributes or "not provided",
                      basis=basis, catalog_attrs=attrs, signals=signals_json,
                      checklist=", ".join(CATEGORY_CHECKLISTS.get(
                          claim.category, ["model", "color", "condition"])))
              ])
  resp = client().models.generate_content(
      model=DEEP_MODEL,
      contents=contents,
      config=types.GenerateContentConfig(
          response_mime_type="application/json",
          response_schema=ModelComparison,
          temperature=0.0,
          thinking_config=types.ThinkingConfig(
              thinking_level=os.environ.get("QC_THINKING", "medium")),
      ),
  )
  return resp.parsed, _cost(DEEP_MODEL, resp)
