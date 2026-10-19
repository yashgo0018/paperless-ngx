from typing import Any

import pytest

from documents.models import CustomField
from paperless_ai.base_model import DocumentClassifierSchema
from paperless_ai.base_model import model_to_classification_suggestions
from paperless_ai.custom_fields import MAX_AI_CUSTOM_FIELDS
from paperless_ai.custom_fields import ai_suggested_custom_fields
from paperless_ai.custom_fields import custom_fields_fingerprint
from paperless_ai.custom_fields import format_custom_fields_for_prompt
from paperless_ai.custom_fields import normalize_custom_field_value

DataType = CustomField.FieldDataType

SELECT_OPTIONS = [
    {"id": "opt-paid", "label": "Paid"},
    {"id": "opt-open", "label": "Unpaid"},
]


def _field(data_type: str, **extra_data: Any) -> CustomField:
    if data_type == DataType.SELECT:
        extra_data.setdefault("select_options", SELECT_OPTIONS)
    return CustomField(pk=1, name="Field", data_type=data_type, extra_data=extra_data)


class TestNormalizeCustomFieldValue:
    @pytest.mark.parametrize(
        ("data_type", "raw", "expected"),
        [
            (DataType.STRING, "  Policy 42 ", "Policy 42"),
            (DataType.STRING, "x" * 200, "x" * 128),
            (DataType.LONG_TEXT, "Some notes", "Some notes"),
            (DataType.URL, "https://example.com/a", "https://example.com/a"),
            (DataType.URL, "not a url", None),
            (DataType.DATE, "2026-03-31", "2026-03-31"),
            (DataType.DATE, "2026-03-31T10:00:00", "2026-03-31"),
            (DataType.DATE, "31.03.2026", None),
            (DataType.BOOL, "true", True),
            (DataType.BOOL, "No", False),
            (DataType.BOOL, "maybe", None),
            (DataType.INT, "1,234", 1234),
            (DataType.INT, "12.5", None),
            (DataType.INT, "99999999999", None),
            (DataType.FLOAT, "12,5", 12.5),
            (DataType.FLOAT, "1.234,50", 1234.5),
            (DataType.FLOAT, "abc", None),
            (DataType.SELECT, "unpaid", "opt-open"),
            (DataType.SELECT, "opt-paid", "opt-paid"),
            (DataType.SELECT, "Overdue", None),
            (DataType.STRING, "", None),
            (DataType.STRING, None, None),
        ],
    )
    def test_converts_or_drops(
        self,
        data_type: str,
        raw: Any,
        expected: Any,
    ) -> None:
        assert normalize_custom_field_value(_field(data_type), raw) == expected

    @pytest.mark.parametrize(
        ("raw", "default_currency", "expected"),
        [
            ("EUR1234.5", None, "EUR1234.50"),
            ("1,234.50 USD", None, "USD1234.50"),
            ("€ 12,99", None, "EUR12.99"),
            ("EUR-12", None, "EUR-12.00"),
            ("12.30", "GBP", "GBP12.30"),
            ("12.30", None, "12.30"),
            ("twelve euros", None, None),
            ("EUR99999999999", None, None),
        ],
    )
    def test_monetary(
        self,
        raw: str,
        default_currency: str | None,
        expected: str | None,
    ) -> None:
        field = _field(DataType.MONETARY, default_currency=default_currency)

        assert normalize_custom_field_value(field, raw) == expected


@pytest.mark.django_db
class TestAISuggestedCustomFields:
    def test_only_enabled_suggestible_fields(self) -> None:
        enabled = CustomField.objects.create(
            name="Total",
            data_type=DataType.MONETARY,
            extra_data={"ai_suggest": True},
        )
        CustomField.objects.create(name="Notes", data_type=DataType.STRING)
        CustomField.objects.create(
            name="Off",
            data_type=DataType.STRING,
            extra_data={"ai_suggest": False},
        )
        CustomField.objects.create(
            name="Related",
            data_type=DataType.DOCUMENTLINK,
            extra_data={"ai_suggest": True},
        )

        assert ai_suggested_custom_fields() == [enabled]

    def test_capped(self) -> None:
        for i in range(MAX_AI_CUSTOM_FIELDS + 2):
            CustomField.objects.create(
                name=f"Field {i}",
                data_type=DataType.STRING,
                extra_data={"ai_suggest": True},
            )

        assert len(ai_suggested_custom_fields()) == MAX_AI_CUSTOM_FIELDS


class TestFormatCustomFieldsForPrompt:
    def test_lists_id_name_format_options_and_hint(self) -> None:
        total = CustomField(
            pk=3,
            name="Invoice total",
            data_type=DataType.MONETARY,
            extra_data={"ai_hint": "  Including   VAT "},
        )
        status = CustomField(
            pk=4,
            name="Status",
            data_type=DataType.SELECT,
            extra_data={"select_options": SELECT_OPTIONS},
        )

        assert format_custom_fields_for_prompt([total, status]) == (
            '- ID 3: "Invoice total" - an amount with its 3-letter currency '
            "code, e.g. EUR1234.50. Including VAT\n"
            '- ID 4: "Status" - exactly one of the listed options '
            '("Paid", "Unpaid")'
        )

    def test_no_fields(self) -> None:
        assert format_custom_fields_for_prompt([]) == ""


def test_fingerprint_changes_with_configuration() -> None:
    field = _field(DataType.STRING, ai_hint="Policy number")
    before = custom_fields_fingerprint([field])

    field.extra_data["ai_hint"] = "Contract number"

    assert custom_fields_fingerprint([field]) != before
    assert custom_fields_fingerprint([field]) == custom_fields_fingerprint([field])


class TestModelCustomFieldValues:
    def test_keeps_values_for_listed_fields_once(self) -> None:
        """
        GIVEN:
            - A model response with values for a listed field (twice), an
              unlisted field and a blank value
        WHEN:
            - It is converted to the internal suggestions
        THEN:
            - Only the first value of each listed field is kept
        """
        model = DocumentClassifierSchema(
            title="Invoice",
            custom_field_ids=[3, 3, 99, 4],
            custom_field_values=["EUR12.00", "EUR13.00", "x", "  "],
        )

        suggestions = model_to_classification_suggestions(
            model,
            {"custom_fields": {3, 4}},
        )

        assert suggestions["custom_fields"] == [{"field": 3, "value": "EUR12.00"}]
