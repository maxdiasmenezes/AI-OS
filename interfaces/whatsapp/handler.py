"""
Classifies one already-authorized, already-deduplicated inbound message
into a work task, and processes tasks pulled from the worker queue:
either by calling the orchestrator (text) or sending a fixed reply
(empty, oversized, or unsupported message).

Destination/sender authorization and deduplication happen earlier, in
interfaces/whatsapp/server.py's synchronous POST handling, before a task
is ever constructed here - nothing in this module performs either. No
retries anywhere in this module, and no log line here ever includes a
sender ID (masked or otherwise), message text, AI response text, or any
detail of an exception - only a generic category (processing_error,
outbound_failure).
"""

import logging

from kernel.models.base import ModelResponse
from kernel.orchestrator.context import RequestContext

from interfaces.whatsapp.client import WhatsAppClientError

logger = logging.getLogger(__name__)

# Milestone 33: every TextTask reaching this module has already passed
# WhatsApp's own exact-sender authorization, synchronously, in
# server.py's POST handling, before it was ever queued - see
# server.py's module docstring. This is the one and only place this
# interface grants computer-action trust; it never depends on, or
# duplicates, the phone-number check itself. See
# kernel/orchestrator/context.py for why the default is deny.
_TRUSTED_CONTEXT = RequestContext(allow_computer_actions=True, actor="whatsapp")

# AI-OS application limits enforced by this interface - not claims about
# any WhatsApp platform limit. Both are injectable via MessageHandler /
# classify_message for testing.
MAX_INCOMING_TEXT_LENGTH = 4096
MAX_OUTGOING_TEXT_LENGTH = 4096

EMPTY_MESSAGE_REPLY = "I didn't receive any text in that message - please send some text."
OVERSIZED_MESSAGE_REPLY = (
    "That message is too long for me to process - please send something shorter."
)
UNSUPPORTED_MESSAGE_REPLY = "I can only handle plain text messages right now."

# Sent instead of a long orchestrator response, never as a truncation of
# it - the original long response is discarded outright, never partially
# sent.
LONG_RESPONSE_NOTICE = (
    "The response was too long to send through this interface. "
    "Please ask a narrower question."
)

# Sent when the orchestrator raises, or returns something unusable (None,
# an unsupported type, or empty/whitespace-only text) - the same fixed
# reply either way, attempted exactly once.
PROCESSING_FAILURE_REPLY = "I could not process that message. Please try again later."


class TextTask:
    """A validated inbound text message, ready for the orchestrator."""

    def __init__(self, sender: str, text: str) -> None:
        self.sender = sender
        self.text = text


class FixedReplyTask:
    """A pre-decided fixed reply - the orchestrator is never called for this."""

    def __init__(self, sender: str, reply_text: str) -> None:
        self.sender = sender
        self.reply_text = reply_text


def classify_message(message, max_incoming_text_length: int = MAX_INCOMING_TEXT_LENGTH):
    """Turn an already-authorized IncomingMessage into a Task. Pure - no I/O,
    no authorization, no deduplication."""

    if message.message_type != "text":
        return FixedReplyTask(message.sender, UNSUPPORTED_MESSAGE_REPLY)

    text = (message.text or "").strip()
    if not text:
        return FixedReplyTask(message.sender, EMPTY_MESSAGE_REPLY)

    if len(text) > max_incoming_text_length:
        return FixedReplyTask(message.sender, OVERSIZED_MESSAGE_REPLY)

    return TextTask(message.sender, text)


def _prepare_outbound_text(
    text: str, max_outgoing_text_length: int = MAX_OUTGOING_TEXT_LENGTH
) -> str:
    """Never truncates: a response over the limit is discarded outright and
    replaced with LONG_RESPONSE_NOTICE."""

    if len(text) > max_outgoing_text_length:
        return LONG_RESPONSE_NOTICE
    return text


def _extract_response_text(result) -> str | None:
    """Return the text to send back, or None if `result` counts as a
    processing failure: None, an unsupported return type, an empty or
    whitespace-only plain string, or a ModelResponse with empty or
    whitespace-only text."""

    if isinstance(result, ModelResponse):
        text = result.text
    elif isinstance(result, str):
        text = result
    else:
        return None

    if not isinstance(text, str) or not text.strip():
        return None

    return text


class MessageHandler:
    """Processes one pre-authorized, already-deduplicated Task at a time.

    Performs no authorization or deduplication itself - by the time a
    Task reaches here, that has already happened synchronously in
    server.py's POST handling.
    """

    def __init__(
        self,
        orchestrator,
        client,
        max_outgoing_text_length: int = MAX_OUTGOING_TEXT_LENGTH,
    ) -> None:
        self._orchestrator = orchestrator
        self._client = client
        self._max_outgoing_text_length = max_outgoing_text_length

    def handle_task(self, task) -> None:
        if isinstance(task, TextTask):
            self._handle_text_task(task)
        elif isinstance(task, FixedReplyTask):
            self._reply(task.sender, task.reply_text)
        else:
            raise TypeError(f"unsupported task type: {type(task).__name__}")

    def _handle_text_task(self, task: TextTask) -> None:
        try:
            result = self._orchestrator.handle(task.text, context=_TRUSTED_CONTEXT)
        except Exception:
            # Never log the exception object, its message, or a traceback -
            # a real (or synthetic) exception's text could itself carry
            # sender IDs, user text, or secrets.
            logger.warning("processing_error")
            self._reply(task.sender, PROCESSING_FAILURE_REPLY)
            return

        text = _extract_response_text(result)
        if text is None:
            logger.warning("processing_error")
            self._reply(task.sender, PROCESSING_FAILURE_REPLY)
            return

        reply_text = _prepare_outbound_text(text, self._max_outgoing_text_length)
        self._reply(task.sender, reply_text)

    def _reply(self, sender: str, body: str) -> None:
        try:
            self._client.send_text_message(sender, body)
        except WhatsAppClientError:
            logger.warning("outbound_failure")
