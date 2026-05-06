# Disease Pipeline Scaffolder

> Single command to spin up the SQL + API + hooks for a new NCD disease.

When we ported diabetes → hypertension we copy-pasted ~30 places. This
scaffolder takes a typed `DiseaseSpec` and emits all the boilerplate
deterministically, so the next disease (CVD, obesity, dyslipidemia,
stroke …) is config-only.

## What it generates

For one disease `<key>`:

| File | Purpose |
|---|---|
| `bma-health-db/db/migrations/<NNN>_mv_<key>.sql` | 4 materialized views |
| `bma-health-db/api/routers/<key>.py`             | FastAPI router (3 endpoints) |
| `bma-health/frontend/src/hooks/use<X>Classification.ts`   | React Query hook |
| `bma-health/frontend/src/hooks/use<X>Factors.ts`           | React Query hook |
| `bma-health/frontend/src/hooks/use<X>FactorsBulk.ts`       | React Query hook |

Plus in-place patches to:

| File | What gets appended |
|---|---|
| `mapStore.ts` | `<X>Pattern` type, `selected<X>Pattern` state + setter |
| `i18n.ts`     | 6 chip label keys (Th/En + locale fallbacks) |

## Usage

```bash
cd /Users/dev/bma-health-db

# 1. Add a DiseaseSpec entry to scaffold/diseases.py (~30 lines)
#    See "Adding a disease" below.

# 2. Preview what would be written
python3 -m scaffold.scaffold cvd --dry-run

# 3. Diff against existing files (useful when iterating)
python3 -m scaffold.scaffold cvd --diff

# 4. Write all files (refuses to overwrite by default)
python3 -m scaffold.scaffold cvd

# 5. Apply migration
docker exec -i bma-health-db psql -U postgres -d bma_health \
    -f /tmp/<NNN>_mv_cvd.sql
# (copy migration into container first: docker cp ...)

# 6. Register router in api/main.py:
#    from api.routers import cvd
#    app.include_router(cvd.router)

# 7. Restart API; flush Redis cache
docker exec bma-health-redis redis-cli FLUSHDB
```

## Adding a disease

Edit `scaffold/diseases.py` and append a `DiseaseSpec` entry:

```python
DISEASES["cvd"] = DiseaseSpec(
    key="cvd",
    short_upper="CVD",
    name_th="หลอดเลือดหัวใจ",
    name_en="Cardiovascular",
    emoji="💗",
    heatmap_key="cardiovascular",   # must match DISEASE_REGISTRY in constants.ts

    # 4-bit pattern source columns
    c1_risk_col="risk_cvd",          # boolean column on mv_visit_resolved
    c2_diag_col="cvd",               # column in app1_/portal_ tables
    c3_family_col="phrtm",           # parent-heart column in homehealth

    chip_disease_word_th="หลอดเลือดหัวใจ",
    chip_disease_word_en="CVD",

    lab=LabAxis(
        name="chol_high",
        sql_app1="""SELECT patient_id, (cholest >= 240) AS chol_high
                    FROM bma_med.app1_labhealth
                    WHERE patient_id IS NOT NULL""",
        sql_portal="""SELECT patient_id,
                      (CASE WHEN cholest ~ '^[0-9]+(\\.[0-9]+)?$'
                            THEN cholest::numeric >= 240 END) AS chol_high
                      FROM bma_med.portal_labhealth
                      WHERE patient_id IS NOT NULL""",
        chip_id="chol",
        chip_label_th="ผลแลป Chol ≥ 240",
        chip_label_en="Chol ≥ 240",
        headline_subtitle_lab_th="Chol ≥ 240",
        headline_subtitle_lab_en="Chol ≥ 240",
    ),
    newly_found=NewlyFoundCohort(
        cohort_label_th="เจอใหม่ในโครงการ — หลอดเลือดหัวใจจากการคัดกรอง",
        cohort_label_en="Newly found via screening — CVD",
        criteria_th='เกณฑ์: ติ้กว่า "ไม่เป็นโรคหัวใจ" + Chol ≥ 240 mg/dL',
        criteria_en="Criteria: self-reported NOT-CVD + Chol ≥ 240 mg/dL",
    ),
    migration_number=230,
)
```

Then run `python3 -m scaffold.scaffold cvd`.

### LabAxis tips

The `c4` axis is the only thing that genuinely differs between diseases.
Three common shapes:

* **Single-column threshold** (e.g. FPG ≥ 126):
  Lab values come from labhealth tables. App1 is numeric, Portal is text
  needing `::numeric` cast.

* **Vital-sign threshold from `mv_visit_resolved`** (e.g. BP ≥ 140/90):
  No app1/portal split needed — `mv_visit_resolved` already unions.
  Set `sql_portal=""` to skip the portal CTE.

* **Composite OR** (e.g. lipid panel: Chol ≥ 240 OR LDL ≥ 160):
  Just write the OR in the CTE body. `bool_or()` collapses by patient.

## Pattern semantics (fixed by design)

* `c1` = risk      — pre-derived `risk_<key>` flag on `mv_visit_resolved`
* `c2` = diag      — patient self-reported having the disease
* `c3` = family    — **parent** had the disease (column starts with `p`)
* `c4` = lab       — bespoke per disease via `LabAxis`

`has_<key>` (used in factor cross-tabs) = `c1 OR c2 OR c4` only.
**Family is excluded** because it's hereditary risk, not active disease
state, and including it makes the family-history cross-tab tautologically
100%. Family stays as its own axis in the 4-bit pattern and as its own
factor row.

`any_<key>_signal` (the "ทั้งหมด" chip total) = `c1 OR c2 OR c4` for the
same reason.

`newly_found_<key>` (Active Follow-up cohort) = `c2=0 AND c4=1`
— self-reported NOT having the disease (c2 = self-reported diagnosed
must be 0) but lab confirmed it (c4 = 1). c1 (risk) can be anything —
what matters is the patient hasn't been told they have the disease yet.

## Disease coverage status

**4-axis NCD pipeline (all auto-wired):**

| Disease | Status | Notes |
|---|---|---|
| เบาหวาน (DM) | ✅ Live | FBS ≥ 126 |
| ความดันโลหิตสูง (HPT) | ✅ Live | SBP ≥ 140 / DBP ≥ 90 |
| หลอดเลือดหัวใจ (CVD) | ✅ Live | Cholesterol ≥ 240 |
| หลอดเลือดสมอง (Stroke) | ✅ Live | EKG abnormal — clinical caveat: lab-axis prevalence high |

**Pending — needs new screening pipeline (1-axis lab-only):**

These diseases don't fit the 4-axis pattern because they lack
self-report (c2) or family-history (c3) columns. They need a simpler
"screening" scaffolder mode that emits only the lab axis:

| Disease | Lab signal | Status |
|---|---|---|
| ไขมันในเลือดสูง | Cholesterol ≥ 200 | TODO — partial fit (`chltr` self-report exists, no family) |
| โรคอ้วน | BMI ≥ 23 | TODO — pure screening (no self-report/family) |
| โรคไต (CKD) | eGFR < 60 | TODO — could be 4-axis (`kidney` + `pkidney` exist) |
| โรคตับ | SGOT/SGPT ≥ 120 | TODO — pure screening |
| ภาวะโลหิตจาง | Hb < 13 (M) / < 12 (F) | TODO — uses `cbcrs` result code |
| X-ray | Chest X-ray abnormal | TODO — pure screening |
| มะเร็งปากมดลูก | Pap smear abnormal | TODO — pure screening |
| มะเร็งลำไส้ | FOBT/colonoscopy abnormal | TODO — pure screening |

**Architecture for screening pipeline (next iteration):**
- `ScreeningSpec` config — simpler than `DiseaseSpec` (no c1/c2/c3 fields)
- One MV per disease: `mv_<key>_screening (district_code, n_total, n_abnormal, abnormal_pct)`
- One API endpoint: `/api/v2/<key>/screening?scope=zone|city|district|non_bkk`
- One TS hook: `useXScreening`
- Simpler tooltip card: just "X% ผิดปกติ (n=Y)" — no 4-axis breakdown
- Separate frontend chip section: "🔬 การคัดกรอง" alongside "❤️ NCDs"

## Known limitations

* The mapStore patcher uses regex anchors that match the *current* style of
  `selected{X}Pattern` declarations. If you reformat that section of the
  store, the anchors may stop matching and you'll get a `RuntimeError`. Run
  with `--diff` first to preview before writing.
* `--force` overwrites existing files including hand-tuned ones. Use it
  only when you've verified the generated output matches what you want
  (run `--diff` first).

## What auto-wires (zero manual code per disease)

These now read from `PIPELINE_DISEASES` registry — no edits needed when
adding a disease:

* **Backend** — SQL migration, FastAPI router, Redis cache flush, main.py registration
* **Frontend hooks** — `useXClassification`, `useXFactors`, `useXFactorsBulk`
* **mapStore** — `XPattern` type, `selectedXPattern` state + setter
* **i18n** — 6 chip labels (Th/En + locale fallbacks)
* **DiseaseControls** — chip filter section (driven by `spec.chipIds`)
* **StatisticsBoard** — Wilson CI inferential card + box plot title
* **ZoneTooltip** — 4-axis card + Active Follow-up callout

## What still needs ~3-5 lines per disease

* `BangkokMap.tsx` — add a zone-scope `useXClassification` call and
  pass `xClassification` + `xCityNewlyFound` props through to the
  tooltip render (~3 lines).
* `NonBkkLayer.tsx` — same, plus add a `useXFactorsBulk` call for the
  non-BKK factor mini-panel (~5 lines total).

These two files iterate over `PIPELINE_DISEASES` for prop spreading but
React rules-of-hooks require static hook calls — until we move them
behind a generated wrapper hook (next iteration), they stay slightly
disease-aware.

## Testing

The scaffolder was validated by regenerating DM and HPT from config and
applying the output to the live database — both produced row counts that
match the hand-written migrations:

```text
mv_dm_classification     | 799
mv_dm_factors            | 160
mv_dm_factors_district   | 1000
mv_dm_factors_region     | 80
mv_hpt_classification    | 800
mv_hpt_factors           | 160
mv_hpt_factors_district  | 1000
mv_hpt_factors_region    | 80
```
