"""AI suggestions for custom field values.

A custom field opts in through its ``extra_data``: ``ai_suggest`` enables it,
``ai_hint`` optionally tells the model what to look for. The model returns a
string per field; normalize_custom_field_value() turns it into a value of the
field's data type, or drops it.
"""

import datetime
import hashlib
import logging
import re
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any
from typing import Final

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from documents.models import CustomField

logger = logging.getLogger("paperless_ai.custom_fields")

AI_SUGGEST_KEY: Final = "ai_suggest"
AI_HINT_KEY: Final = "ai_hint"
# Keeps the prompt and the structured response small
MAX_AI_CUSTOM_FIELDS: Final = 10
MAX_AI_HINT_LENGTH: Final = 200
# Longest suggested long text value, a summary rather than a transcript
MAX_LONG_TEXT_LENGTH: Final = 1000

# Document links point at other documents, which the model cannot know
SUGGESTIBLE_DATA_TYPES: Final = frozenset(
    data_type
    for data_type in CustomField.FieldDataType
    if data_type != CustomField.FieldDataType.DOCUMENTLINK
)

_FORMAT_HINTS: Final = {
    CustomField.FieldDataType.STRING: "short text",
    CustomField.FieldDataType.LONG_TEXT: "text",
    CustomField.FieldDataType.URL: "a full URL",
    CustomField.FieldDataType.DATE: "a date, YYYY-MM-DD",
    CustomField.FieldDataType.BOOL: "true or false",
    CustomField.FieldDataType.INT: "a whole number",
    CustomField.FieldDataType.FLOAT: "a number",
    CustomField.FieldDataType.MONETARY: (
        "an amount with its 3-letter currency code, e.g. EUR1234.50"
    ),
    CustomField.FieldDataType.SELECT: "exactly one of the listed options",
}

_BOOLEANS: Final = {
    "true": True,
    "yes": True,
    "1": True,
    "false": False,
    "no": False,
    "0": False,
}
_CURRENCY_SYMBOLS: Final = {"€": "EUR", "$": "USD", "£": "GBP", "₹": "INR", "¥": "JPY"}
_MONETARY_RE: Final = re.compile(
    r"^(?P<pre>[A-Z]{3})?\s*(?P<amount>-?\d+(?:\.\d+)?)\s*(?P<post>[A-Z]{3})?$",
)
# 1,234 or 1.234.567: a whole number with thousands separators
_THOUSANDS_RE: Final = re.compile(r"-?\d{1,3}(?:[.,]\d{3})+")
# Matches the range CustomFieldInstanceSerializer accepts for integers
_INT_MIN: Final = -2147483648
_INT_MAX: Final = 2147483647


def ai_suggested_custom_fields() -> list[CustomField]:
    """The custom fields to suggest values for, oldest first."""
    return list(
        CustomField.objects.filter(
            **{f"extra_data__{AI_SUGGEST_KEY}": True},
            data_type__in=SUGGESTIBLE_DATA_TYPES,
        ).order_by("created", "pk")[:MAX_AI_CUSTOM_FIELDS],
    )


def custom_fields_fingerprint(fields: list[CustomField]) -> str:
    """Identifies the fields' AI configuration, so cached suggestions are
    not reused once it changes."""
    config = ";".join(
        f"{field.pk}:{field.data_type}:{field.name}:{_hint(field)}:"
        f"{','.join(str(option.get('id', '')) for option in _select_options(field))}"
        for field in fields
    )
    return hashlib.sha256(config.encode()).hexdigest()[:16]


def format_custom_fields_for_prompt(fields: list[CustomField]) -> str:
    """One line per field: its ID, name, expected format and hint."""
    lines = []
    for field in fields:
        line = f'- ID {field.pk}: "{field.name}" - {_FORMAT_HINTS[field.data_type]}'
        if field.data_type == CustomField.FieldDataType.SELECT:
            labels = ", ".join(
                f'"{option.get("label")}"' for option in _select_options(field)
            )
            line += f" ({labels})"
        hint = _hint(field)
        if hint:
            line += f". {hint}"
        lines.append(line)
    return "\n".join(lines)


def normalize_custom_field_value(field: CustomField, raw: Any) -> Any:
    """``raw``, the model's answer for ``field``, as a value of the field's
    data type, or None when it is empty or not a valid value.
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    try:
        match field.data_type:
            case CustomField.FieldDataType.STRING:
                return text[:128]
            case CustomField.FieldDataType.LONG_TEXT:
                return text[:MAX_LONG_TEXT_LENGTH]
            case CustomField.FieldDataType.URL:
                URLValidator()(text)
                return text
            case CustomField.FieldDataType.DATE:
                return datetime.date.fromisoformat(text[:10]).isoformat()
            case CustomField.FieldDataType.BOOL:
                return _BOOLEANS[text.lower()]
            case CustomField.FieldDataType.INT:
                if _THOUSANDS_RE.fullmatch(text):
                    text = text.replace(",", "").replace(".", "")
                number = Decimal(_plain_number(text))
                if number != number.to_integral_value():
                    return None
                value = int(number)
                return value if _INT_MIN <= value <= _INT_MAX else None
            case CustomField.FieldDataType.FLOAT:
                return float(Decimal(_plain_number(text)))
            case CustomField.FieldDataType.MONETARY:
                return _normalize_monetary(field, text)
            case CustomField.FieldDataType.SELECT:
                return _select_option_id(field, text)
    except (ValueError, KeyError, InvalidOperation, ValidationError):
        pass
    logger.debug(
        "Dropping AI suggestion %r for custom field %s (%s)",
        text,
        field.pk,
        field.data_type,
    )
    return None


def _hint(field: CustomField) -> str:
    hint = (field.extra_data or {}).get(AI_HINT_KEY) or ""
    return " ".join(str(hint).split())[:MAX_AI_HINT_LENGTH]


def _select_options(field: CustomField) -> list[dict]:
    return (field.extra_data or {}).get("select_options") or []


def _plain_number(text: str) -> str:
    """``text`` without thousands separators, with a decimal point."""
    text = text.replace(" ", "").replace("\u00a0", "")
    if "," in text and "." in text:
        # The later separator is the decimal one: 1,234.50 or 1.234,50
        thousands = "," if text.rfind(".") > text.rfind(",") else "."
        text = text.replace(thousands, "")
    return text.replace(",", ".")


def _normalize_monetary(field: CustomField, text: str) -> str | None:
    for symbol, code in _CURRENCY_SYMBOLS.items():
        text = text.replace(symbol, f" {code} ")
    text = " ".join(text.upper().split())
    match = _MONETARY_RE.match(_plain_number(text.replace(" ", "")))
    if match is None:
        return None
    amount = Decimal(match["amount"]).quantize(Decimal("0.01"))
    if abs(amount) >= Decimal("1e10"):
        # Monetary values allow 12 digits, 2 of them decimals
        return None
    currency = (
        match["pre"]
        or match["post"]
        or (field.extra_data or {}).get("default_currency")
        or ""
    )
    return f"{currency.upper()}{amount}"


def _select_option_id(field: CustomField, text: str) -> str | None:
    options = _select_options(field)
    for option in options:
        if text == option.get("id"):
            return option["id"]
    folded = text.casefold()
    for option in options:
        if folded == str(option.get("label", "")).strip().casefold():
            return option["id"]
    return None
