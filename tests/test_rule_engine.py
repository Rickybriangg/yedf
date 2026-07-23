"""Unit tests for the safe condition evaluator."""
import pytest

from app.rule_engine import ConditionError, match_condition, validate_condition

FACTS = {
    "loanNo": "L-1",
    "tier": "Curable",
    "arrearsBucket": "1-30",
    "dormancyDays": 35,
    "daysPastDue": 2,
    "daysSinceLastAction": 10,
    "product": "Business",
    "outstandingBalance": 15000,
}


def test_tier_match_true():
    assert match_condition(FACTS, {"tier": "Curable"}) is True


def test_tier_match_false():
    assert match_condition(FACTS, {"tier": "Doubtful"}) is False


def test_day_range_match_inclusive():
    assert match_condition(FACTS, {"daysPastDue": {"gte": 1, "lte": 3}}) is True
    assert match_condition(FACTS, {"daysPastDue": {"gte": 3}}) is False
    assert match_condition(FACTS, {"daysPastDue": {"lt": 2}}) is False
    assert match_condition(FACTS, {"daysPastDue": {"lte": 2}}) is True


def test_multiple_keys_are_anded():
    assert match_condition(FACTS, {"tier": "Curable", "daysPastDue": {"lte": 3}}) is True
    assert match_condition(FACTS, {"tier": "Curable", "daysPastDue": {"gte": 5}}) is False


def test_list_membership():
    assert match_condition(FACTS, {"arrearsBucket": ["1-30", "31-60"]}) is True
    assert match_condition(FACTS, {"arrearsBucket": ["61-90"]}) is False


def test_in_and_nin_operators():
    assert match_condition(FACTS, {"product": {"in": ["Business", "Sme"]}}) is True
    assert match_condition(FACTS, {"product": {"nin": ["Personal"]}}) is True


def test_ne_operator():
    assert match_condition(FACTS, {"tier": {"ne": "Doubtful"}}) is True


def test_unknown_field_raises():
    with pytest.raises(ConditionError):
        match_condition(FACTS, {"notAField": 1})


def test_unknown_operator_raises():
    with pytest.raises(ConditionError):
        match_condition(FACTS, {"daysPastDue": {"between": [1, 3]}})


def test_missing_fact_does_not_match_comparison():
    # daysSinceLastAction present, but pretend it's missing.
    facts = dict(FACTS)
    facts["daysSinceLastAction"] = None
    assert match_condition(facts, {"daysSinceLastAction": {"gte": 1}}) is False


def test_type_mismatch_raises_clean_error():
    with pytest.raises(ConditionError):
        match_condition(FACTS, {"tier": {"gte": 5}})  # str vs int


def test_empty_condition_matches_everything():
    assert match_condition(FACTS, {}) is True


def test_validate_condition_ok():
    validate_condition({"tier": "Curable", "daysPastDue": {"gte": 1}})


def test_validate_condition_rejects_bad_operator():
    with pytest.raises(ConditionError):
        validate_condition({"daysPastDue": {"approx": 3}})
