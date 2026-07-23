"""
Thin urllib-based client for the WhatsApp Cloud API.

The only module that knows the Cloud API's HTTP shape - sending a text
message is a POST to /{api_version}/{phone_number_id}/messages with a
bearer token. Mirrors kernel/models/ollama.py's use of urllib.request
rather than adding an HTTP dependency. `urlopen` is left injectable so
tests never need a real network call. Exactly one attempt is made - no
retries - and nothing here ever logs or raises an error containing the
access token, recipient, request body, or response body.
"""

import json
import urllib.error
import urllib.request

_GRAPH_API_BASE_URL = "https://graph.facebook.com"
DEFAULT_TIMEOUT_SECONDS = 10


class WhatsAppClientError(Exception):
    """Raised when a Cloud API request fails, or its response is unusable."""


class WhatsAppClient:
    """Sends outbound WhatsApp text messages via the Cloud API. No retries."""

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        urlopen=urllib.request.urlopen,
    ) -> None:
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._api_version = api_version
        self._timeout_seconds = timeout_seconds
        self._urlopen = urlopen

    def send_text_message(self, to: str, body: str) -> str:
        """Send a single outbound text message.

        Returns the Cloud API's outbound message ID. Raises
        WhatsAppClientError if the request fails or the response does not
        contain a usable message ID. Makes exactly one attempt.
        """

        payload = json.dumps({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }).encode("utf-8")

        request = urllib.request.Request(
            f"{_GRAPH_API_BASE_URL}/{self._api_version}/{self._phone_number_id}/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._access_token}",
            },
            method="POST",
        )

        try:
            with self._urlopen(request, timeout=self._timeout_seconds) as response:
                raw_response = response.read()
        except urllib.error.URLError as error:
            raise WhatsAppClientError("failed to send outbound WhatsApp message") from error

        try:
            parsed = json.loads(raw_response)
            message_id = parsed["messages"][0]["id"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            raise WhatsAppClientError(
                "Cloud API response did not contain a usable message ID"
            ) from error

        if not isinstance(message_id, str) or not message_id:
            raise WhatsAppClientError("Cloud API response did not contain a usable message ID")

        return message_id
