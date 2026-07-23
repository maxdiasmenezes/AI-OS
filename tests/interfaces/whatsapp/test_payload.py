"""Tests for conservative WhatsApp webhook payload parsing."""

from interfaces.whatsapp.payload import parse_webhook_payload


def _envelope(messages, phone_number_id="1234567890"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "entry-1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "15550001111",
                        "phone_number_id": phone_number_id,
                    },
                    "messages": messages,
                },
            }],
        }],
    }


def test_parses_a_single_text_message():
    body = _envelope([
        {"id": "wamid.1", "from": "15551234567", "type": "text", "text": {"body": "hello"}},
    ])

    parsed = parse_webhook_payload(body)

    assert len(parsed) == 1
    message = parsed[0]
    assert message.message_id == "wamid.1"
    assert message.sender == "15551234567"
    assert message.phone_number_id == "1234567890"
    assert message.message_type == "text"
    assert message.text == "hello"


def test_parses_multiple_messages_in_one_batch():
    body = _envelope([
        {"id": "wamid.1", "from": "15551234567", "type": "text", "text": {"body": "first"}},
        {"id": "wamid.2", "from": "15551234567", "type": "text", "text": {"body": "second"}},
    ])

    parsed = parse_webhook_payload(body)

    assert [m.text for m in parsed] == ["first", "second"]


def test_non_text_message_is_parsed_with_no_text():
    body = _envelope([
        {"id": "wamid.1", "from": "15551234567", "type": "image", "image": {"id": "media-1"}},
    ])

    parsed = parse_webhook_payload(body)

    assert len(parsed) == 1
    assert parsed[0].message_type == "image"
    assert parsed[0].text is None


def test_status_only_payload_yields_no_messages():
    body = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "entry-1",
            "changes": [{
                "field": "messages",
                "value": {"statuses": [{"id": "wamid.1", "status": "delivered"}]},
            }],
        }],
    }

    assert parse_webhook_payload(body) == []


def test_message_missing_required_fields_is_skipped():
    body = _envelope([
        {"from": "15551234567", "type": "text", "text": {"body": "no id"}},
    ])

    assert parse_webhook_payload(body) == []


def test_malformed_top_level_shapes_do_not_raise():
    assert parse_webhook_payload({}) == []
    assert parse_webhook_payload({"entry": "not-a-list"}) == []
    assert parse_webhook_payload({"entry": [{"changes": "not-a-list"}]}) == []
    assert parse_webhook_payload({"entry": [{"changes": [{"value": "not-a-dict"}]}]}) == []
    assert parse_webhook_payload("not-a-dict") == []
