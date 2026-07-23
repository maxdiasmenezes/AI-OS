"""
Conservative parsing of WhatsApp Cloud API webhook POST payloads.

Meta's webhook shape supports batching (multiple entries/changes/messages
per request) and multiple message types, plus unrelated event types
(status/delivery updates) sharing the same envelope. This module only
picks out actual inbound messages and treats the rest of the shape as
untrusted and possibly missing: every access is defensive, and a
malformed or unrecognized entry is skipped rather than raising, since one
bad entry should never take down parsing of the rest of the batch.
"""


class IncomingMessage:
    """One inbound WhatsApp message, extracted from a webhook payload."""

    def __init__(
        self,
        message_id: str,
        sender: str,
        phone_number_id: str | None,
        message_type: str,
        text: str | None,
    ) -> None:
        self.message_id = message_id
        self.sender = sender
        self.phone_number_id = phone_number_id
        self.message_type = message_type
        self.text = text


def parse_webhook_payload(body) -> list:
    """Extract every inbound message from a webhook payload, skipping the rest."""

    messages = []

    if not isinstance(body, dict):
        return messages

    for entry in body.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue

            metadata = value.get("metadata")
            phone_number_id = metadata.get("phone_number_id") if isinstance(metadata, dict) else None

            for raw_message in value.get("messages") or []:
                parsed = _parse_message(raw_message, phone_number_id)
                if parsed is not None:
                    messages.append(parsed)

    return messages


def _parse_message(raw_message, phone_number_id):
    if not isinstance(raw_message, dict):
        return None

    message_id = raw_message.get("id")
    sender = raw_message.get("from")
    message_type = raw_message.get("type")

    if not message_id or not sender or not message_type:
        return None

    text = None
    if message_type == "text":
        text_field = raw_message.get("text")
        if isinstance(text_field, dict):
            text = text_field.get("body")

    return IncomingMessage(
        message_id=message_id,
        sender=sender,
        phone_number_id=phone_number_id,
        message_type=message_type,
        text=text,
    )
