"""Tests for the Graph API error hints surfaced in logs."""

from app.whatsapp_client import _graph_error_hint


def test_hint_for_unsupported_method_points_at_phone_number_id():
    body = '{"error":{"message":"Unsupported request - method type: post","code":100,"type":"GraphMethodException"}}'
    hint = _graph_error_hint(body)
    assert "WHATSAPP_PHONE_NUMBER_ID" in hint
    assert "WABA" in hint


def test_hint_for_authentication_error_points_at_token():
    body = '{"error":{"message":"Authentication Error","code":190,"type":"OAuthException"}}'
    hint = _graph_error_hint(body)
    assert "WHATSAPP_API_TOKEN" in hint


def test_no_hint_for_unrelated_errors():
    assert _graph_error_hint('{"error":{"message":"some other failure"}}') == ""
