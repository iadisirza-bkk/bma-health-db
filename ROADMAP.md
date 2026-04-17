# ROADMAP.md — BMA Health Database

> **Last Updated:** 2026-04-17 | **Version:** 4.5.0

Legend: ✅ Done | ⚠️ Partial / Has Issues | ❌ Not Implemented

---

## 1. Statistical Analysis Tools

| Feature | Status | Notes |
|---------|--------|-------|
| ✅ Chi-Square Test | Done | Disease × sex/age/behavior cross-tabulation, p-value |
| ✅ One-Way ANOVA | Done | F-statistic, p-value, eta-squared across districts |
| ✅ Odds Ratio | Done | OR with 95% CI, 2×2 contingency tables |
| ✅ Pearson Correlation | Done | District-level cross-disease correlation matrix |
| ✅ Comorbidity Matrix | Done | All disease-pair co-occurrence + metabolic syndrome |
| ⚠️ Logistic Regression | Partial | Approximation via OR + SE, not full GLM (statsmodels needed) |
| ⚠️ Mann-Kendall Trend Test | Partial | **Uses simulated monthly data** — needs real time-series from raw_vitalsigns |
| ❌ Fisher Exact Test | Not implemented | Needed for small-sample cross-tabs |
| ❌ Survival Analysis | Not implemented | Kaplan-Meier / Cox regression for disease progression |
| ❌ Multi-level Modeling | Not implemented | Hierarchical: patient → district → zone |

---

## 2. Chatbot / Agents

| Feature | Status | Notes |
|---------|--------|-------|
| ✅ Multi-Agent Pipeline | Done | Analyst → Tool Execution → Synthesizer (SSE streaming) |
| ✅ 7 Agent Tools | Done | query_health_data, query_api, statistical_test, report, adaptive_report, zone_info, clarification |
| ✅ SSE Streaming | Done | Real-time token streaming with agent status animation |
| ✅ Topic Guardrails | Done | 2-layer: keyword filter + system prompt scope enforcement |
| ✅ Prompt Injection Defense | Done | System prompt blocks role-override attempts |
| ✅ Circuit Breaker | Done | 3-failure threshold, 60s recovery, auto-fallback |
| ✅ Rule-Based Fallback | Done | 8 intent patterns: overview, prevalence, lab_values, advice, etc. |
| ✅ Keyword Router | Done | Routes to correct tool by Thai/English keywords |
| ✅ Forced Tool Calls | Done | If LLM skips tools, forces top-priority tool from router |
| ✅ No-Hallucination Guard | Done | Refuses if tool returns "ไม่พบข้อมูล" instead of letting synthesizer fabricate |
| ✅ Synthesizer Strict Mode | Done | Must copy exact numbers from tool output, no rounding/inventing |
| ⚠️ Conversation Memory | Partial | Last 2-4 turns only, truncated to 400 chars |
| ⚠️ Multi-Tool Chaining | Partial | LLM can call 1 tool per turn; complex queries may need follow-up |
| ❌ User Feedback Loop | Not implemented | No thumbs-up/down or correction mechanism |
| ❌ Suggested Follow-ups | Not implemented | AI-generated next questions based on context |

---

## 3. Document Generation

| Feature | Status | Notes |
|---------|--------|-------|
| ✅ Whitepaper (TH/EN) | Done | Full health analysis PDF via LaTeX/Tectonic |
| ✅ Executive Slides (TH/EN) | Done | Beamer slide deck with charts |
| ✅ MSD Comprehensive Report | Done | 100+ page deep-dive, 10 parts |
| ✅ Disease-Specific Slides | Done | 9 diseases × per-disease ranking PDF |
| ✅ Zone Reports (8 zones) | Done | Per-zone slide deck with district breakdown |
| ✅ Adaptive AI Reports | Done | LLM-written content → custom PDF/slides |
| ✅ Chart Generation | Done | 20+ chart types (donut, bar, radar, heatmap, forest plot, scatter) |
| ✅ Nightly Scheduler | Done | Auto-generates all reports at 00:30 daily |
| ✅ Hash-Based Caching | Done | Skips rebuild if data unchanged |
| ✅ Report Catalog API | Done | Lists all reports with cache status + download URLs |
| ⚠️ Screening Test Data | Partial | Now uses real DB data (was fake 70/20/10 split) — verify accuracy |
| ⚠️ PM2.5 Section | Partial | Now uses live ArcGIS API (was completely fake) — seasonal analysis removed |
| ⚠️ Diet/Vaccination Data | Partial | Now queries raw_homehealth (was hardcoded) — some fields may be sparse |
| ❌ Public Infographic | Not implemented | 1-page visual summary for public distribution |
| ❌ Multi-language Beyond TH/EN | Not implemented | Templates exist for 8 languages but untested |

---

## 4. API Endpoints

| Feature | Status | Notes |
|---------|--------|-------|
| ✅ Summary (5 endpoints) | Done | Overview, filtered, lab, mental health, demographics |
| ✅ Zones (3 endpoints) | Done | Zone dashboards, zone detail, zone comparison |
| ✅ Districts (3 endpoints) | Done | District list, detail, disease detail |
| ✅ Epidemiology (6 endpoints) | Done | Age prevalence, age pyramid, disease-lab crosstab, multi-disease matrix, incidence rate, outbreak detection |
| ✅ Trends (2 endpoints) | Done | Screening trends, disease trends (2024+) |
| ✅ Search (1 endpoint) | Done | District search/ranking by disease |
| ✅ KPI (7 endpoints) | Done | MOPH targets, screening yield, benchmarks |
| ✅ Executive (5 endpoints) | Done | Headline KPI, YoY comparison (2024+), media brief |
| ✅ GIS/PM2.5 (12 endpoints) | Done | Live ArcGIS PM2.5, facility coords, heatmap overlays |
| ✅ Dashboards (3 endpoints) | Done | Governor, director, medical views |
| ✅ Statistics (6 endpoints) | Done | City overview, district compare, ranking |
| ✅ Factors (8 endpoints) | Done | Sex, age, occupation, behavior, cross-tabulation |
| ✅ Screening Tests (6 endpoints) | Done | EKG, X-ray, blood, retinal summaries |
| ✅ Reports API (16 endpoints) | Done | Generate, download, catalog, dashboard, progress |
| ✅ Admin API (6 endpoints) | Done | Upload screening/Excel, data status, cache, audit log |
| ✅ Admin Panel (12 endpoints) | Done | Dashboard, upload, bundle upload, history, data quality, logs |
| ✅ Monitoring (7 endpoints) | Done | Data quality, ETL status, cache stats |
| ⚠️ Promotion (6 endpoints) | Partial | BMI/exercise done; alcohol, waist, salt, fitness — pending HDC data |
| ⚠️ Disease Control (6 endpoints) | Partial | NCD cascade done; repeat screening, disease progression, referral outcome — missing longitudinal data |
| ⚠️ Research (6 endpoints) | Partial | Correlation, stats test done; individual data export — access-controlled |
| ⚠️ Facility (6 endpoints) | Partial | Performance done; staff performance — no HR data |
| ⚠️ Public (7 endpoints) | Partial | Locations, health tips done; service satisfaction, complaint — no feedback data |
| ❌ Campaign Impact | Not implemented | Needs campaign_events table |

---

## 5. MCP Server / Medical AI Tools

| Feature | Status | Notes |
|---------|--------|-------|
| ✅ FastMCP Server | Done | Async MCP framework with thread-pooled PostgreSQL |
| ✅ 8+ Analysis Tools | Done | overview, district, zone, disease ranking, lab values, correlation, outbreak detection |
| ✅ Audit Logging | Done | SHA-256 chained tamper-detection audit trail |
| ✅ Security | Done | Blocks raw tables, k-anonymity, PII filtered, read-only user |
| ✅ Shared Data Service | Done | Same HealthDataService as REST API |
| ⚠️ Tool Schema Validation | Partial | Input validation exists but not comprehensive |
| ❌ Multi-Turn Agent Memory | Not implemented | Each MCP call is stateless |
| ❌ Custom Tool Registration | Not implemented | Tools are hardcoded, no plugin system |

---

## 6. Medical Analysis

| Feature | Status | Notes |
|---------|--------|-------|
| ✅ NCD Cascade | Done | Screened → At-Risk → Diagnosed pipeline for all 9 diseases |
| ✅ Outbreak Detection | Done | Baseline mean + 2SD threshold, monthly time-series |
| ✅ Incidence Rate | Done | Monthly new cases / total screened × 100% |
| ✅ Disease Correlation | Done | Pearson r between all disease pairs across 50 districts |
| ✅ Comorbidity Analysis | Done | DM+HPT, DM+obesity, metabolic syndrome counts from DB |
| ✅ Disease-Lab Cross-Tab | Done | FBS by DM status, SBP by HPT status, cholesterol by dyslipidemia |
| ✅ Age-Group Prevalence | Done | Disease rates stratified by 7 age groups |
| ✅ Risk Factor Stratification | Done | Smoking, alcohol, exercise × disease prevalence |
| ✅ BMI Distribution | Done | Underweight/normal/overweight/obese/severely obese from DB |
| ✅ Screening Coverage | Done | Per-district coverage vs 1.6M target |
| ⚠️ Repeat Screening Analysis | Partial | Basic visit count distribution; no clinical follow-up tracking |
| ⚠️ PM2.5 × Disease Correlation | Partial | PM2.5 data live from ArcGIS; seasonal disease correlation removed (was fake) |
| ❌ Disease Progression Modeling | Not implemented | Needs longitudinal patient tracking |
| ❌ Treatment Outcome Tracking | Not implemented | No treatment/follow-up data in current schema |
| ❌ Referral Pathway Analysis | Not implemented | Needs referral_events table |
| ❌ Spatial Hotspot Detection | Not implemented | GIS clustering (Getis-Ord Gi*) not built |
| ❌ Predictive Risk Scoring | Not implemented | ML-based individual risk prediction |
| ❌ Cost-Effectiveness Analysis | Not implemented | QALY/DALY calculations need outcome data |

---

## Data Readiness

| Data Source | Status | Records |
|------------|--------|---------|
| ✅ Patient demographics | Available | 446K patients |
| ✅ Vitalsigns/screening | Available | 480K records |
| ✅ Lab results | Available | ~400K records |
| ✅ Home visit | Available | ~370K records |
| ✅ Home health | Available | ~430K records |
| ✅ Lab extended (MSD) | Available | ~400K records |
| ✅ PM2.5 (realtime) | Available | Live from ArcGIS (~85 stations) |
| ✅ Facility reference | Available | 14K+ facilities |
| ✅ District/Zone reference | Available | 50 districts, 8 zones |
| ⚠️ Time-series depth | Limited | Most data from 2024-2026 only |
| ⚠️ Alcohol/diet detail | Sparse | HDC data integration pending |
| ❌ Treatment/follow-up | Missing | No post-diagnosis tracking |
| ❌ Referral records | Missing | No referral_events table |
| ❌ Campaign events | Missing | No campaign_events table |
| ❌ Patient satisfaction | Missing | No feedback/survey data |

---

## Priority Roadmap

### Phase 1 — Quick Wins (1-2 weeks)
- [ ] Fix Mann-Kendall to use real time-series from raw_vitalsigns visits
- [ ] Add Fisher Exact Test for small-sample cross-tabs
- [ ] Implement suggested follow-up questions in chat
- [ ] Public infographic report (1-page visual summary)

### Phase 2 — Data Enhancement (2-4 weeks)
- [ ] HDC data integration (alcohol, detailed nutrition)
- [ ] Repeat screening clinical tracking (same patient, lab value changes)
- [ ] Campaign events table + impact analysis endpoint
- [ ] Patient satisfaction/feedback system

### Phase 3 — Advanced Analytics (1-2 months)
- [ ] Full logistic regression (statsmodels GLM)
- [ ] Spatial hotspot detection (Getis-Ord Gi*)
- [ ] PM2.5 × disease seasonal correlation (real data)
- [ ] Disease progression modeling (longitudinal)

### Phase 4 — Research Grade (3+ months)
- [ ] Multi-level hierarchical modeling
- [ ] Predictive risk scoring (ML)
- [ ] Cost-effectiveness analysis (QALY/DALY)
- [ ] Survival analysis (Kaplan-Meier / Cox)
- [ ] Treatment outcome tracking
