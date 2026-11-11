# QC Return-Fraud Demo: Core Engine

Shared engine (`qc_core`) used by the GE workflow and the Return QC Workbench (MCP App).

```
qc_demo/
├── qc_core/
│   ├── schemas.py     # Claim, Verdict, reason codes, routes (the contract)
│   ├── checks.py      # pHash reuse index + EXIF anomaly checks (deterministic)
│   ├── gemini.py      # Flash quality gate + Argon deep comparison (schema-constrained JSON)
│   ├── pipeline.py    # gate -> basis (dispatch/video/catalogue) -> checks -> Argon -> policy
│   └── evidence.py    # side-by-side evidence image with boxes + verdict banner
├── data/
│   ├── claims.yaml    # 18 staged claim cases + expected outcomes
│   ├── SHOT_LIST.md   # what to photograph/film
│   ├── media/         # photos/videos (placeholders until real captures)
│   └── known_images/  # stock/previously seen images for reuse detection
└── harness/
    ├── make_placeholders.py
    └── run.py         # batch run + scorecard + cost/scale projection
```

## Setup (your machine / Cloud Shell)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pillow imagehash pydantic google-genai pyyaml
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=agent-builder-ge GOOGLE_CLOUD_LOCATION=global
```

## Run
```bash
python harness/make_placeholders.py        # only until real photos exist
python -m harness.run --dry-run            # plumbing test (fake model outputs)
python -m harness.run --only C01,C13,C16   # LIVE on a few image cases
python -m harness.run                      # LIVE, all 18 (needs the 6 videos)
python -m harness.run --phase SHADOW       # pilot mode: everything to humans
```

Config (env): `QC_FLASH_MODEL` (default `gemini-3.8-flash`), `QC_DEEP_MODEL` (`gemini-4-argon`),
`QC_THINKING` (`medium`), `QC_PHASE` (`FULL|AUTO_APPROVE|SHADOW`), `QC_HIGH_VALUE` (15000),
`QC_AUDIT_RATE` (0.05), price overrides `QC_DEEP_IN/OUT`, `QC_FLASH_IN/OUT`.

> Cost figures: Argon uses the introductory list price from the Gemini 4 sales FAQ ($2/$10 per 1M tokens).
> The **Flash price is a placeholder**: confirm it on the pricing page before showing cost numbers.

## Routing policy (decision #16)
| Condition | Route |
|---|---|
| Claim value ≥ `QC_HIGH_VALUE` | HIGH_VALUE (human) |
| Phase SHADOW | FLAGGED (human confirms everything) |
| Confidence < 0.90 or model says HUMAN_REVIEW | FLAGGED |
| Basis = CATALOG_LISTING | FLAGGED |
| APPROVE ≥ 0.90 | AUTO_APPROVE (5% → AUDIT_SAMPLE) |
| REQUEST_MORE_EVIDENCE ≥ 0.90 | AUTO_REQUEST_EVIDENCE |
| REJECT ≥ 0.90 | AUTO_REJECT in FULL, FLAGGED otherwise |
| Photo unusable (Flash gate) | RETAKE |

Deterministic reuse/EXIF hits can only make a verdict **more cautious** (APPROVE → REQUEST_MORE_EVIDENCE).
