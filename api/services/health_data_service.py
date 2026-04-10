"""
Shared health data service — used by both REST API routers AND MCP server.
Encapsulates all business logic for health screening data queries.

This is the SINGLE source of truth for data access logic.
Both the REST API and MCP server call these methods.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Union


class HealthDataService:
    """Shared service for health screening data.

    Accepts query callables via constructor so both:
    - REST API (database.py pool) and
    - MCP server (its own pool with security validation)
    can share the same business logic.
    """

    TARGET_SCREENED = 1_600_000
    K_ANONYMITY_THRESHOLD = 5

    DISEASE_KEY_MAP = {
        "diabetes": {"pct_at_risk": "pct_risk_dm", "pct_found": "pct_found_dm", "risk_count": "risk_dm_count", "found_count": "found_dm_count", "risk_col": "risk_dm"},
        "hypertension": {"pct_at_risk": "pct_risk_hpt", "pct_found": "pct_found_hpt", "risk_count": "risk_hpt_count", "found_count": "found_hpt_count", "risk_col": "risk_hpt"},
        "cardiovascular": {"pct_at_risk": "pct_risk_cvd", "pct_found": "pct_found_cvd", "risk_count": "risk_cvd_count", "found_count": "found_cvd_count", "risk_col": "risk_cvd"},
        "obesity": {"pct_at_risk": "pct_risk_bmi", "pct_found": "found_obesity_count", "risk_col": "risk_bmi"},
        "dyslipidemia": {"pct_found": "found_dyslipidemia_count", "risk_col": "found_dyslipidemia"},
        "stroke": {"pct_found": "found_stroke_count", "risk_col": "found_stroke"},
    }

    VALID_DISEASE_KEYS = set(DISEASE_KEY_MAP.keys())

    def __init__(
        self,
        query: Callable[..., List[Dict]],
        scalar: Callable[..., Any],
        query_trend: Optional[Callable[..., List[Dict]]] = None,
    ):
        """
        Args:
            query: Function to execute SELECT and return list of dicts.
            scalar: Function to execute and return single value.
            query_trend: Optional function for raw table trend queries (MCP uses
                        a special path that validates GROUP BY). If None, falls
                        back to `query`.
        """
        self._q = query
        self._s = scalar
        self._qt = query_trend or query

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _round_floats(obj: Any, decimals: int = 2) -> Any:
        if isinstance(obj, float):
            return round(obj, decimals)
        if isinstance(obj, dict):
            return {k: HealthDataService._round_floats(v, decimals) for k, v in obj.items()}
        if isinstance(obj, list):
            return [HealthDataService._round_floats(item, decimals) for item in obj]
        return obj

    def _enforce_k_anonymity(self, rows: List[Dict], count_col: str = "patient_count") -> List[Dict]:
        return [r for r in rows if (r.get(count_col) or 0) >= self.K_ANONYMITY_THRESHOLD]

    def _validate_disease_key(self, key: str) -> str:
        key = key.strip().lower()
        if key not in self.VALID_DISEASE_KEYS:
            raise ValueError(f"Invalid disease_key '{key}'. Valid: {sorted(self.VALID_DISEASE_KEYS)}")
        return key

    # ------------------------------------------------------------------
    # Tool 1: get_overview
    # ------------------------------------------------------------------

    def get_overview(self) -> dict:
        rows = self._q("""
            SELECT
                SUM(total_screened) AS total_screened,
                COUNT(DISTINCT zone_code) AS zones,
                COUNT(*) AS districts
            FROM summary_district_disease
        """)
        result = dict(rows[0]) if rows else {}
        result["target"] = self.TARGET_SCREENED
        result["last_updated"] = datetime.now(timezone.utc).isoformat()
        return self._round_floats(result)

    # ------------------------------------------------------------------
    # Tool 2: get_zone_summary
    # ------------------------------------------------------------------

    def get_zone_summary(self, zone_code: str) -> dict:
        zone_code = str(zone_code).strip()
        rows = self._q("""
            SELECT district_code, district_name, zone_code, total_screened,
                   pct_risk_dm, pct_found_dm, pct_risk_hpt, pct_found_hpt,
                   pct_risk_cvd, pct_found_cvd,
                   risk_bmi_count, found_obesity_count, found_dyslipidemia_count, found_stroke_count
            FROM summary_district_disease
            WHERE zone_code = %s
            ORDER BY district_code
        """, (zone_code,))

        if not rows:
            return {"error": f"No data found for zone_code='{zone_code}'"}

        total_screened = sum(r["total_screened"] or 0 for r in rows)
        districts = [
            {"dcode": r["district_code"], "name_th": r["district_name"], "total_screened": r["total_screened"]}
            for r in rows
        ]

        def _weighted_pct(col: str):
            total = sum((r.get(col) or 0) * (r["total_screened"] or 0) for r in rows)
            return round(total / total_screened, 2) if total_screened else None

        diseases = {
            "diabetes": {"pct_risk": _weighted_pct("pct_risk_dm"), "pct_found": _weighted_pct("pct_found_dm")},
            "hypertension": {"pct_risk": _weighted_pct("pct_risk_hpt"), "pct_found": _weighted_pct("pct_found_hpt")},
            "cardiovascular": {"pct_risk": _weighted_pct("pct_risk_cvd"), "pct_found": _weighted_pct("pct_found_cvd")},
        }

        return self._round_floats({
            "zone_code": zone_code,
            "total_screened": total_screened,
            "districts": districts,
            "diseases": diseases,
        })

    # ------------------------------------------------------------------
    # Tool 3: get_district_summary
    # ------------------------------------------------------------------

    def get_district_summary(self, dcode: str) -> dict:
        dcode = str(dcode).strip()

        disease_rows = self._q("""
            SELECT district_code, district_name, zone_code, total_screened,
                   risk_dm_count, risk_hpt_count, risk_cvd_count, risk_bmi_count,
                   found_dm_count, found_hpt_count, found_cvd_count, found_stroke_count,
                   found_obesity_count, found_dyslipidemia_count,
                   pct_risk_dm, pct_risk_hpt, pct_risk_cvd,
                   pct_found_dm, pct_found_hpt, pct_found_cvd
            FROM summary_district_disease WHERE district_code = %s
        """, (dcode,))

        if not disease_rows:
            return {"error": f"No data found for district code='{dcode}'"}
        if (disease_rows[0].get("total_screened") or 0) < self.K_ANONYMITY_THRESHOLD:
            return {"error": "Data suppressed for privacy (k-anonymity threshold)"}

        d = disease_rows[0]

        lab_rows = self._q("""
            SELECT total_lab_patients, avg_hemoglobin, avg_hematocrit, avg_fbs,
                   avg_cholesterol, avg_triglyceride, avg_hdl, avg_ldl,
                   avg_creatinine, avg_egfr, avg_uric_acid, avg_sgot, avg_sgpt,
                   pct_anemia, pct_ckd, pct_cbc_abnormal, pct_liver_abnormal
            FROM summary_district_lab WHERE district_code = %s
        """, (dcode,))
        lab = lab_rows[0] if lab_rows else {}

        mental_rows = self._q("""
            SELECT total_screened, pct_depression_risk, pct_phq9_moderate, pct_high_stress
            FROM summary_district_mental WHERE district_code = %s
        """, (dcode,))
        mental = mental_rows[0] if mental_rows else {}

        return self._round_floats({
            "dcode": dcode,
            "name_th": d.get("district_name"),
            "zone_code": d.get("zone_code"),
            "total_screened": d.get("total_screened"),
            "diseases": {
                "diabetes": {"pct_risk": d.get("pct_risk_dm"), "pct_found": d.get("pct_found_dm")},
                "hypertension": {"pct_risk": d.get("pct_risk_hpt"), "pct_found": d.get("pct_found_hpt")},
                "cardiovascular": {"pct_risk": d.get("pct_risk_cvd"), "pct_found": d.get("pct_found_cvd")},
                "obesity": {"found_count": d.get("found_obesity_count")},
                "dyslipidemia": {"found_count": d.get("found_dyslipidemia_count")},
                "stroke": {"found_count": d.get("found_stroke_count")},
            },
            "lab_summary": {
                "total_lab_patients": lab.get("total_lab_patients"),
                "avg_hemoglobin": lab.get("avg_hemoglobin"),
                "avg_fbs": lab.get("avg_fbs"),
                "avg_cholesterol": lab.get("avg_cholesterol"),
                "avg_triglyceride": lab.get("avg_triglyceride"),
                "avg_hdl": lab.get("avg_hdl"),
                "avg_ldl": lab.get("avg_ldl"),
                "avg_creatinine": lab.get("avg_creatinine"),
                "avg_egfr": lab.get("avg_egfr"),
                "pct_anemia": lab.get("pct_anemia"),
                "pct_ckd": lab.get("pct_ckd"),
            },
            "mental_health": {
                "total_screened": mental.get("total_screened"),
                "pct_depression_risk": mental.get("pct_depression_risk"),
                "pct_phq9_moderate": mental.get("pct_phq9_moderate"),
                "pct_high_stress": mental.get("pct_high_stress"),
            },
        })

    # ------------------------------------------------------------------
    # Tool 4: compare_disease
    # ------------------------------------------------------------------

    def compare_disease(
        self, disease_key: str, level: str = "zone", codes: Optional[List[str]] = None,
    ) -> Union[List[Dict], Dict]:
        disease_key = self._validate_disease_key(disease_key)
        level = level.strip().lower()
        if level not in ("zone", "district"):
            return {"error": "level must be 'zone' or 'district'"}

        mapping = self.DISEASE_KEY_MAP[disease_key]

        if level == "zone":
            sql = "SELECT zone_code AS code, zone_code AS name_th, SUM(total_screened) AS total_screened"
            if "pct_at_risk" in mapping:
                risk_count_col = mapping.get("risk_count", "risk_dm_count")
                sql += f", ROUND(100.0 * SUM({risk_count_col}) / NULLIF(SUM(total_screened), 0), 2) AS pct_at_risk"
            else:
                sql += ", NULL AS pct_at_risk"
            sql += " FROM summary_district_disease"
            params: list = []
            if codes:
                placeholders = ",".join(["%s"] * len(codes))
                sql += f" WHERE zone_code IN ({placeholders})"
                params.extend(codes)
            sql += " GROUP BY zone_code ORDER BY zone_code"
        else:
            pct_col = mapping.get("pct_at_risk")
            select_pct = f"{pct_col} AS pct_at_risk" if pct_col and pct_col.startswith("pct_") else "NULL AS pct_at_risk"
            sql = f"SELECT district_code AS code, district_name AS name_th, total_screened, {select_pct} FROM summary_district_disease"
            params = []
            if codes:
                placeholders = ",".join(["%s"] * len(codes))
                sql += f" WHERE district_code IN ({placeholders})"
                params.extend(codes)
            sql += " ORDER BY district_code"

        rows = self._q(sql, params or None)
        rows = [r for r in rows if (r.get("total_screened") or 0) >= self.K_ANONYMITY_THRESHOLD]

        sorted_rows = sorted(rows, key=lambda r: r.get("pct_at_risk") or 0, reverse=True)
        for rank, row in enumerate(sorted_rows, 1):
            row["rank"] = rank

        return self._round_floats(sorted_rows)

    # ------------------------------------------------------------------
    # Tool 5: get_filtered_summary
    # ------------------------------------------------------------------

    def get_filtered_summary(self, filters: Optional[Dict] = None) -> Union[List[Dict], Dict]:
        allowed = {"dcode", "sex", "age_group", "smoking", "exercise"}
        col_map = {"dcode": "district_code", "sex": "sex", "age_group": "age_group", "smoking": "smoking", "exercise": "exercise"}

        conditions = []
        params: list = []
        for key, value in (filters or {}).items():
            key = key.strip().lower()
            if key not in allowed:
                continue
            conditions.append(f"{col_map[key]} = %s")
            params.append(str(value))

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = f"""
            SELECT district_code, sex, age_group, smoking, exercise,
                   SUM(patient_count) AS patient_count,
                   ROUND(AVG(avg_sbp)::numeric, 2) AS avg_sbp,
                   ROUND(AVG(avg_dbp)::numeric, 2) AS avg_dbp,
                   ROUND(AVG(avg_weight_kg)::numeric, 2) AS avg_weight_kg,
                   ROUND(AVG(avg_waist_cm)::numeric, 2) AS avg_waist_cm,
                   ROUND(AVG(avg_bmi)::numeric, 2) AS avg_bmi
            FROM summary_district_risk_factors
            {where_clause}
            GROUP BY district_code, sex, age_group, smoking, exercise
            ORDER BY district_code, sex, age_group
        """

        rows = self._q(sql, params or None)
        safe_rows = self._enforce_k_anonymity(rows, count_col="patient_count")
        suppressed = len(rows) - len(safe_rows)

        if not safe_rows:
            return {"error": "All result groups have fewer than 5 people. Cannot return data to protect privacy.", "k_anonymity_threshold": self.K_ANONYMITY_THRESHOLD}

        return {
            "rows": self._round_floats(safe_rows),
            "total_rows": len(safe_rows),
            "has_suppressed_data": suppressed > 0,
            "k_anonymity_threshold": self.K_ANONYMITY_THRESHOLD,
        }

    # ------------------------------------------------------------------
    # Tool 6: get_trend
    # ------------------------------------------------------------------

    def get_trend(
        self, disease_key: str, dcode: Optional[str] = None, granularity: str = "monthly",
    ) -> Union[List[Dict], Dict]:
        disease_key = self._validate_disease_key(disease_key)
        granularity = granularity.strip().lower()
        if granularity not in ("monthly", "quarterly"):
            return {"error": "granularity must be 'monthly' or 'quarterly'"}

        risk_col = self.DISEASE_KEY_MAP[disease_key].get("risk_col")
        if not risk_col:
            return {"error": f"Trend data not available for '{disease_key}'"}

        trunc = "month" if granularity == "monthly" else "quarter"
        conditions = ["v.cancel_status IS DISTINCT FROM 1"]
        params: list = []

        if dcode:
            conditions.append("v.district_code = %s")
            params.append(str(dcode).strip())

        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT
                DATE_TRUNC('{trunc}', v.visit_date) AS period,
                COUNT(DISTINCT v.patient_id) AS total_screened,
                ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.{risk_col})
                    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_at_risk
            FROM raw_vitalsigns v
            WHERE {where_clause}
            GROUP BY DATE_TRUNC('{trunc}', v.visit_date)
            HAVING COUNT(DISTINCT v.patient_id) >= %s
            ORDER BY period
        """
        params.append(self.K_ANONYMITY_THRESHOLD)

        rows = self._qt(sql, params)
        for row in rows:
            if row.get("period") and hasattr(row["period"], "isoformat"):
                row["period"] = row["period"].isoformat()

        return self._round_floats(rows)

    # ------------------------------------------------------------------
    # Tool 7: get_lab_summary
    # ------------------------------------------------------------------

    def get_lab_summary(self, dcode: Optional[str] = None, zone_code: Optional[str] = None) -> dict:
        conditions: list[str] = []
        params: list = []

        if dcode:
            conditions.append("l.district_code = %s")
            params.append(str(dcode).strip())
        elif zone_code:
            conditions.append("l.district_code IN (SELECT district_code FROM summary_district_disease WHERE zone_code = %s)")
            params.append(str(zone_code).strip())

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = f"""
            SELECT
                SUM(total_lab_patients) AS total_lab_patients,
                ROUND((SUM(avg_hemoglobin * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_hemoglobin,
                ROUND((SUM(avg_hematocrit * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_hematocrit,
                ROUND((SUM(avg_fbs * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_fbs,
                ROUND((SUM(avg_cholesterol * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_cholesterol,
                ROUND((SUM(avg_triglyceride * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_triglyceride,
                ROUND((SUM(avg_hdl * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_hdl,
                ROUND((SUM(avg_ldl * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_ldl,
                ROUND((SUM(avg_creatinine * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_creatinine,
                ROUND((SUM(avg_egfr * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_egfr,
                ROUND((SUM(avg_uric_acid * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_uric_acid,
                ROUND((SUM(avg_sgot * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_sgot,
                ROUND((SUM(avg_sgpt * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS avg_sgpt,
                ROUND((SUM(pct_anemia * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS pct_anemia,
                ROUND((SUM(pct_ckd * total_lab_patients) / NULLIF(SUM(total_lab_patients), 0))::numeric, 2) AS pct_ckd
            FROM summary_district_lab l
            {where_clause}
        """
        rows = self._q(sql, params or None)
        result = dict(rows[0]) if rows else {}

        if (result.get("total_lab_patients") or 0) < self.K_ANONYMITY_THRESHOLD:
            return {"error": "Data suppressed for privacy (k-anonymity threshold)"}

        return self._round_floats(result)

    # ------------------------------------------------------------------
    # Tool 8: get_mental_health_summary
    # ------------------------------------------------------------------

    def get_mental_health_summary(self, dcode: Optional[str] = None, zone_code: Optional[str] = None) -> dict:
        conditions: list[str] = []
        params: list = []

        if dcode:
            conditions.append("m.district_code = %s")
            params.append(str(dcode).strip())
        elif zone_code:
            conditions.append("m.district_code IN (SELECT district_code FROM summary_district_disease WHERE zone_code = %s)")
            params.append(str(zone_code).strip())

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = f"""
            SELECT
                SUM(total_screened) AS total_screened,
                ROUND((SUM(pct_depression_risk * total_screened) / NULLIF(SUM(total_screened), 0))::numeric, 2) AS pct_depression_risk,
                ROUND((SUM(pct_phq9_moderate * total_screened) / NULLIF(SUM(total_screened), 0))::numeric, 2) AS pct_phq9_moderate,
                ROUND((SUM(pct_high_stress * total_screened) / NULLIF(SUM(total_screened), 0))::numeric, 2) AS pct_high_stress
            FROM summary_district_mental m
            {where_clause}
        """
        rows = self._q(sql, params or None)
        result = dict(rows[0]) if rows else {}

        if (result.get("total_screened") or 0) < self.K_ANONYMITY_THRESHOLD:
            return {"error": "Data suppressed for privacy (k-anonymity threshold)"}

        return self._round_floats(result)

    # ------------------------------------------------------------------
    # Tool 9: get_demographics
    # ------------------------------------------------------------------

    def get_demographics(self, dcode: Optional[str] = None, zone_code: Optional[str] = None) -> dict:
        conditions: list[str] = []
        params: list = []

        if dcode:
            conditions.append("d.district_code = %s")
            params.append(str(dcode).strip())
        elif zone_code:
            conditions.append("d.district_code IN (SELECT district_code FROM summary_district_disease WHERE zone_code = %s)")
            params.append(str(zone_code).strip())

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = f"""
            SELECT SUM(total_respondents) AS total_respondents,
                   SUM(edu_none) AS edu_none, SUM(edu_primary) AS edu_primary,
                   SUM(edu_secondary) AS edu_secondary, SUM(edu_high_school) AS edu_high_school,
                   SUM(edu_vocational) AS edu_vocational, SUM(edu_bachelor) AS edu_bachelor,
                   SUM(edu_postgrad) AS edu_postgrad,
                   SUM(occ_government) AS occ_government, SUM(occ_private) AS occ_private,
                   SUM(occ_self_employed) AS occ_self_employed, SUM(occ_agriculture) AS occ_agriculture,
                   SUM(occ_unemployed) AS occ_unemployed, SUM(occ_student) AS occ_student,
                   SUM(occ_retired) AS occ_retired,
                   SUM(priv_ucs) AS priv_ucs, SUM(priv_sso) AS priv_sso,
                   SUM(priv_csmbs) AS priv_csmbs, SUM(priv_other) AS priv_other,
                   SUM(house_owned) AS house_owned, SUM(house_rented) AS house_rented,
                   SUM(house_condo) AS house_condo, SUM(house_other) AS house_other
            FROM summary_district_demographics d
            {where_clause}
        """
        rows = self._q(sql, params or None)
        r = dict(rows[0]) if rows else {}
        total = r.get("total_respondents") or 0

        if total < self.K_ANONYMITY_THRESHOLD:
            return {"error": "Data suppressed for privacy (k-anonymity threshold)"}

        def _pct(val):
            return round(100.0 * (val or 0) / total, 2) if total else None

        return self._round_floats({
            "total_respondents": total,
            "education_breakdown": {k: {"count": r.get(f"edu_{k}"), "pct": _pct(r.get(f"edu_{k}"))} for k in ("none", "primary", "secondary", "high_school", "vocational", "bachelor", "postgrad")},
            "occupation_breakdown": {k: {"count": r.get(f"occ_{k}"), "pct": _pct(r.get(f"occ_{k}"))} for k in ("government", "private", "self_employed", "agriculture", "unemployed", "student", "retired")},
            "privilege_breakdown": {k: {"count": r.get(f"priv_{k}"), "pct": _pct(r.get(f"priv_{k}"))} for k in ("ucs", "sso", "csmbs", "other")},
            "housing_breakdown": {k: {"count": r.get(f"house_{k}"), "pct": _pct(r.get(f"house_{k}"))} for k in ("owned", "rented", "condo", "other")},
        })

    # ------------------------------------------------------------------
    # Tool 10: search_districts
    # ------------------------------------------------------------------

    def search_districts(self, query: Optional[Dict] = None) -> Union[List[Dict], Dict]:
        if not query:
            return {"error": "query parameter is required"}

        disease = str(query.get("disease", "")).strip().lower()
        min_pct = query.get("min_pct")
        max_pct = query.get("max_pct")
        sort_by = str(query.get("sort_by", "desc")).strip().lower()
        limit = int(query.get("limit", 10))

        disease = self._validate_disease_key(disease)
        if sort_by not in ("asc", "desc"):
            return {"error": "sort_by must be 'asc' or 'desc'"}
        limit = min(max(limit, 1), 100)

        mapping = self.DISEASE_KEY_MAP[disease]
        value_col = mapping.get("pct_at_risk")
        if value_col:
            select_expr = f"{value_col} AS matching_value"
        else:
            found_col = mapping.get("pct_found", mapping.get("found_count"))
            select_expr = f"ROUND(100.0 * {found_col} / NULLIF(total_screened, 0), 2) AS matching_value"

        order = "DESC" if sort_by == "desc" else "ASC"
        params: list = []

        sql = f"""
            WITH ranked AS (
                SELECT district_code AS dcode, district_name AS name_th, zone_code,
                       total_screened, {select_expr}
                FROM summary_district_disease
            )
            SELECT * FROM ranked WHERE matching_value IS NOT NULL
        """
        if min_pct is not None:
            sql += " AND matching_value >= %s"
            params.append(float(min_pct))
        if max_pct is not None:
            sql += " AND matching_value <= %s"
            params.append(float(max_pct))

        sql += f" ORDER BY matching_value {order} LIMIT %s"
        params.append(limit)

        rows = self._q(sql, params)
        rows = [r for r in rows if (r.get("total_screened") or 0) >= self.K_ANONYMITY_THRESHOLD]
        return self._round_floats(rows)
