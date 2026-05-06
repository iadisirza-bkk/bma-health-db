"""Unit tests for the orchestrator's forced-tool selection.

These pin down the fix for the LLM-eval gap where relationship/comorbidity
questions ("X กับ Y สัมพันธ์ไหม") were forced to query_health_data and
returned a useless prevalence donut. The router now considers question
phrasing when picking the fallback tool, and the statistical-tool default
args use a valid `test` enum value.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))

from agents.core.orchestrator import _default_stat_args, _pick_forced_tool  # noqa: E402


# --------------------------------------------------------------------------- #
# _pick_forced_tool
# --------------------------------------------------------------------------- #


class TestPickForcedTool:
    def test_relationship_phrase_prefers_statistical(self):
        tool = _pick_forced_tool(
            "การสูบบุหรี่กับโรคความดันสัมพันธ์กันไหม",
            ["query_health_data", "query_statistical_test", "ask_clarification"],
        )
        assert tool == "query_statistical_test"

    def test_comorbidity_phrase_prefers_statistical(self):
        tool = _pick_forced_tool(
            "มีคนเป็นโรคร่วมหลายโรคพร้อมกันกี่คน",
            ["query_api", "query_health_data", "query_statistical_test"],
        )
        assert tool == "query_statistical_test"

    def test_link_phrase_prefers_statistical(self):
        tool = _pick_forced_tool(
            "BMI สูงกับเบาหวานเชื่อมโยงกันแค่ไหน",
            ["query_api", "query_statistical_test"],
        )
        assert tool == "query_statistical_test"

    def test_no_stat_phrase_uses_first_in_list(self):
        tool = _pick_forced_tool(
            "เบาหวานในแต่ละเขต",
            ["query_health_data", "query_statistical_test"],
        )
        assert tool == "query_health_data"

    def test_stat_phrase_but_stat_tool_not_routed_uses_first(self):
        """Don't force a tool the router didn't surface — it'll be missing
        from the LLM's tool registry filter."""
        tool = _pick_forced_tool(
            "สัมพันธ์",
            ["query_health_data", "ask_clarification"],
        )
        assert tool == "query_health_data"

    def test_empty_list_falls_back_to_query_health_data(self):
        assert _pick_forced_tool("anything", []) == "query_health_data"


# --------------------------------------------------------------------------- #
# _default_stat_args
# --------------------------------------------------------------------------- #


class TestDefaultStatArgs:
    def test_comorbidity_phrasing(self):
        assert _default_stat_args("มีคนเป็นโรคร่วมหลายโรค") == {"test": "comorbidity"}

    def test_pram_phrasing(self):
        assert _default_stat_args("คนพร้อมกันหลายโรค") == {"test": "comorbidity"}

    def test_odds_phrasing(self):
        assert _default_stat_args("odds ratio ของสูบบุหรี่") == {"test": "odds_ratio"}

    def test_risk_phrasing(self):
        assert _default_stat_args("เพิ่มความเสี่ยงของเบาหวาน") == {"test": "odds_ratio"}

    def test_trend_phrasing(self):
        assert _default_stat_args("แนวโน้มเบาหวาน") == {"test": "mann_kendall"}

    def test_correlation_phrasing(self):
        assert _default_stat_args("correlation ของ BMI") == {"test": "correlation"}

    def test_default_is_comorbidity(self):
        # Vague stat question — comorbidity needs no extra args, safe default.
        assert _default_stat_args("ทดสอบสถิติอะไรก็ได้") == {"test": "comorbidity"}

    def test_returned_test_is_valid_enum_value(self):
        """Regression for the bug we fixed: the old default was
        {"test_type": "cross_tabulation"} which is the wrong field name AND
        not a valid enum value, so the tool silently failed."""
        valid = {
            "chi_square", "odds_ratio", "anova", "logistic_regression",
            "correlation", "mann_kendall", "comorbidity",
        }
        for question in (
            "สัมพันธ์", "เชื่อมโยง", "โรคร่วม", "odds", "trend",
            "correlation", "อะไรก็ได้",
        ):
            args = _default_stat_args(question)
            assert "test" in args, f"missing 'test' field for {question!r}"
            assert args["test"] in valid, (
                f"invalid enum value {args['test']!r} for {question!r}"
            )
