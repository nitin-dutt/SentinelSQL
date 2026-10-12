import pytest
from app.utils.masking import (
    mask_email,
    mask_ssn,
    mask_phone,
    mask_credit_card,
    mask_secret,
    mask_financial,
    mask_text_content,
    apply_data_masking
)


def test_mask_email():
    assert mask_email("alice.smith@example.com") == "a*********h@example.com"
    assert mask_email("bob@example.com") == "b*b@example.com"
    assert mask_email("ed@example.com") == "**@example.com"
    assert mask_email("not-an-email") == "not-an-email"


def test_mask_ssn():
    assert mask_ssn("123456789") == "*****6789"
    assert mask_ssn("123-45-6789") == "***-**-6789"


def test_mask_phone():
    assert mask_phone("555-123-4567") == "***-***-4567"
    assert mask_phone("+1 (555) 000-1234") == "+* (***) ***-1234"


def test_mask_credit_card():
    # Formatted with dashes
    assert mask_credit_card("4111-2222-3333-4444") == "****-****-****-4444"
    # Raw 16-digits
    assert mask_credit_card("4111222233334444") == "************4444"
    # Spaces
    assert mask_credit_card("4111 2222 3333 4444") == "**** **** **** 4444"


def test_mask_secret():
    assert mask_secret("super_secret_password_123") == "[REDACTED]"
    assert mask_secret("ghp_928374982374928374") == "[REDACTED]"


def test_mask_financial():
    assert mask_financial(150000) == "[CONFIDENTIAL]"
    assert mask_financial("$250,000") == "[CONFIDENTIAL]"


def test_mask_text_content():
    raw_text = (
        "Client contact is john.doe@example.com, SSN: 123-45-6789, "
        "and card on file is 4111-2222-3333-5555."
    )
    sanitized = mask_text_content(raw_text)
    assert "john.doe@example.com" not in sanitized
    assert "j***e@example.com" in sanitized
    assert "123-45-6789" not in sanitized
    assert "***-**-6789" in sanitized
    assert "4111-2222-3333-5555" not in sanitized
    assert "****-****-****-5555" in sanitized


def test_apply_data_masking_comprehensive():
    payload = {
        "columns": ["id", "customer_name", "email", "ssn", "phone", "credit_card", "salary", "api_token", "notes"],
        "rows": [
            {
                "id": 1,
                "customer_name": "Alice Johnson",
                "email": "alice.johnson@corp.com",
                "ssn": "987654321",
                "phone": "555-888-9999",
                "credit_card": "4111-2222-3333-4444",
                "salary": 185000,
                "api_token": "sk-proj-98273498273",
                "notes": "Email me at support@external.com for verification."
            }
        ]
    }

    result = apply_data_masking(payload)
    row = result["rows"][0]

    # Non-sensitive columns remain intact
    assert row["id"] == 1
    assert row["customer_name"] == "Alice Johnson"

    # Sensitive columns masked according to policy
    assert row["email"] == "a***********n@corp.com"
    assert row["ssn"] == "*****4321"
    assert row["phone"] == "***-***-9999"
    assert row["credit_card"] == "****-****-****-4444"
    assert row["salary"] == "[CONFIDENTIAL]"
    assert row["api_token"] == "[REDACTED]"
    assert "support@external.com" not in row["notes"]
    assert "s***t@external.com" in row["notes"]
