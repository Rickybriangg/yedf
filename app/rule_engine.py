"""Safe, typed condition evaluator for AutomationRule.condition.

No eval(). A condition is a JSON object mapping a known loan *fact* to either:
  - a scalar          -> equality           {"tier": "Curable"}
  - a list            -> membership (in)     {"arrearsBucket": ["1-30", "31-60"]}
  - an operator object -> comparison(s)      {"daysPastDue": {"gte": 1, "lte": 3}}

All keys in a condition are AND-ed together. Unknown fields or operators raise
ConditionError so bad rule config is caught loudly (the engine isolates a broken
rule rather than letting it match by accident or crash the whole run).
"""
from __future__ import annotations

from typing import Any, Mapping

# Loan facts the matcher understands. compute_facts() (automation.py) must
# produce exactly these keys.
ALLOWED_FIELDS = frozenset(
    {
        "loanNo",
        "tier",
        "arrearsBucket",
        "dormancyDays",
        "daysPastDue",
        "daysSinceLastAction",
        "product",
        "outstandingBalance",
    }
)

_OPERATORS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "nin": lambda a, b: a not in b,
}


class ConditionError(ValueError):
    """Raised for a malformed / unsupported condition."""


def _match_field(fact_value: Any, spec: Any) -> bool:
    # Operator object, e.g. {"gte": 1, "lte": 3}
    if isinstance(spec, Mapping):
        for op, operand in spec.items():
            fn = _OPERATORS.get(op)
            if fn is None:
                raise ConditionError(f"Unknown operator '{op}'")
            if fact_value is None:
                # A missing fact can't satisfy a comparison.
                return False
            try:
                if not fn(fact_value, operand):
                    return False
            except TypeError as exc:  # e.g. comparing str > int
                raise ConditionError(
                    f"Cannot apply operator '{op}' to {fact_value!r} and {operand!r}"
                ) from exc
        return True

    # List => membership.
    if isinstance(spec, (list, tuple, set)):
        return fact_value in spec

    # Scalar => equality.
    return fact_value == spec


def match_condition(facts: Mapping[str, Any], condition: Mapping[str, Any]) -> bool:
    """Return True iff every key in `condition` matches the corresponding fact."""
    if not isinstance(condition, Mapping):
        raise ConditionError("Condition must be a JSON object")
    for field, spec in condition.items():
        if field not in ALLOWED_FIELDS:
            raise ConditionError(f"Unknown condition field '{field}'")
        if not _match_field(facts.get(field), spec):
            return False
    return True


def validate_condition(condition: Mapping[str, Any]) -> None:
    """Structural validation without needing real facts — for admin/rule CRUD.
    Raises ConditionError on anything the matcher would reject at run time."""
    if not isinstance(condition, Mapping):
        raise ConditionError("Condition must be a JSON object")
    for field, spec in condition.items():
        if field not in ALLOWED_FIELDS:
            raise ConditionError(f"Unknown condition field '{field}'")
        if isinstance(spec, Mapping):
            for op in spec:
                if op not in _OPERATORS:
                    raise ConditionError(f"Unknown operator '{op}'")
