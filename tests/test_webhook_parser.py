"""Tests for webhook payload parsing helpers."""

from app.webhook_parser import extract_metadata


def test_extract_metadata_returns_sender_number_info():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": "15551604392",
                                "phone_number_id": "1367614196433652",
                            }
                        }
                    }
                ]
            }
        ]
    }

    assert extract_metadata(payload) == [{"display_phone_number": "15551604392", "phone_number_id": "1367614196433652"}]


def test_extract_metadata_is_empty_without_metadata():
    assert extract_metadata({"entry": [{"changes": [{"value": {}}]}]}) == []
    assert extract_metadata({}) == []
