"""
Tests for message classification and task processing.

Authorization and deduplication are no longer this module's job (they
happen synchronously in server.py before a Task is ever created) - these
tests only cover classify_message() and MessageHandler.handle_task().
"""

import logging

from kernel.models.base import ModelResponse

from interfaces.whatsapp.client import WhatsAppClientError
from interfaces.whatsapp.handler import (
    EMPTY_MESSAGE_REPLY,
    LONG_RESPONSE_NOTICE,
    MAX_INCOMING_TEXT_LENGTH,
    MAX_OUTGOING_TEXT_LENGTH,
    OVERSIZED_MESSAGE_REPLY,
    PROCESSING_FAILURE_REPLY,
    UNSUPPORTED_MESSAGE_REPLY,
    FixedReplyTask,
    MessageHandler,
    TextTask,
    classify_message,
)
from interfaces.whatsapp.payload import IncomingMessage

SENDER = "15551234567"
PHONE_NUMBER_ID = "1234567890"


def _model_response(text: str) -> ModelResponse:
    return ModelResponse(
        text=text, model="fake-model", input_tokens=1, output_tokens=1, latency_seconds=0.01
    )


class FakeOrchestrator:
    """Returns whatever `result` is configured with - a plain str, a real
    ModelResponse, an exception to raise, or any other (invalid) value -
    so handle_task()'s contract with Orchestrator.handle() can be tested
    precisely, rather than assumed."""

    def __init__(self, result="a reply"):
        self._result = result
        self.received_prompts: list = []

    def handle(self, prompt: str):
        self.received_prompts.append(prompt)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class RecordingClient:
    def __init__(self):
        self.sent: list = []

    def send_text_message(self, to, body):
        self.sent.append((to, body))
        return "wamid.OUT1"


class FailingClient:
    def send_text_message(self, to, body):
        raise WhatsAppClientError("boom")


def _message(**overrides):
    fields = dict(
        message_id="wamid.1",
        sender=SENDER,
        phone_number_id=PHONE_NUMBER_ID,
        message_type="text",
        text="hello",
    )
    fields.update(overrides)
    return IncomingMessage(**fields)


# --- classify_message(): pure, no I/O ---------------------------------


def test_text_message_produces_a_text_task():
    task = classify_message(_message(text="what wine goes with steak?"))

    assert isinstance(task, TextTask)
    assert task.sender == SENDER
    assert task.text == "what wine goes with steak?"


def test_empty_text_produces_a_fixed_reply_task():
    task = classify_message(_message(text="   "))

    assert isinstance(task, FixedReplyTask)
    assert task.reply_text == EMPTY_MESSAGE_REPLY


def test_oversized_text_produces_a_fixed_reply_task():
    task = classify_message(_message(text="x" * (MAX_INCOMING_TEXT_LENGTH + 1)))

    assert isinstance(task, FixedReplyTask)
    assert task.reply_text == OVERSIZED_MESSAGE_REPLY


def test_unsupported_type_produces_a_fixed_reply_task():
    task = classify_message(_message(message_type="image", text=None))

    assert isinstance(task, FixedReplyTask)
    assert task.reply_text == UNSUPPORTED_MESSAGE_REPLY


def test_incoming_length_limit_is_injectable():
    task = classify_message(_message(text="x" * 10), max_incoming_text_length=5)

    assert isinstance(task, FixedReplyTask)
    assert task.reply_text == OVERSIZED_MESSAGE_REPLY


# --- MessageHandler.handle_task(): no authorization, no dedup ----------


def test_fixed_reply_task_is_sent_without_calling_the_orchestrator():
    orchestrator = FakeOrchestrator("should not be reached")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(FixedReplyTask(SENDER, UNSUPPORTED_MESSAGE_REPLY))

    assert orchestrator.received_prompts == []
    assert client.sent == [(SENDER, UNSUPPORTED_MESSAGE_REPLY)]


def test_response_at_exactly_the_limit_is_not_replaced():
    orchestrator = FakeOrchestrator("z" * MAX_OUTGOING_TEXT_LENGTH)
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "tell me everything"))

    assert client.sent == [(SENDER, "z" * MAX_OUTGOING_TEXT_LENGTH)]


def test_outgoing_length_limit_is_injectable():
    orchestrator = FakeOrchestrator("y" * 10)
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client, max_outgoing_text_length=5)

    handler.handle_task(TextTask(SENDER, "hi"))

    assert client.sent == [(SENDER, LONG_RESPONSE_NOTICE)]


def test_client_failure_while_replying_does_not_raise():
    handler = MessageHandler(FakeOrchestrator(), FailingClient())

    handler.handle_task(TextTask(SENDER, "hello"))  # should not raise


def test_unrecognized_task_type_raises_type_error():
    handler = MessageHandler(FakeOrchestrator(), RecordingClient())

    class NotATask:
        pass

    try:
        handler.handle_task(NotATask())
        assert False, "expected TypeError"
    except TypeError:
        pass


# --- Orchestrator result contract: plain str vs ModelResponse ----------


def test_deterministic_plain_string_response_is_relayed():
    orchestrator = FakeOrchestrator("a bold Malbec would work well")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "what wine goes with steak?"))

    assert orchestrator.received_prompts == ["what wine goes with steak?"]
    assert client.sent == [(SENDER, "a bold Malbec would work well")]


def test_model_response_uses_its_text_field():
    orchestrator = FakeOrchestrator(_model_response("a robust Cabernet"))
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "recommend a wine"))

    assert client.sent == [(SENDER, "a robust Cabernet")]


def test_long_model_response_is_replaced_with_the_fixed_notice_not_truncated():
    orchestrator = FakeOrchestrator(_model_response("y" * 5000))
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "tell me everything"))

    assert client.sent == [(SENDER, LONG_RESPONSE_NOTICE)]


def test_empty_plain_string_response_is_a_processing_failure():
    orchestrator = FakeOrchestrator("")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "hello"))

    assert client.sent == [(SENDER, PROCESSING_FAILURE_REPLY)]


def test_whitespace_only_string_response_is_a_processing_failure():
    orchestrator = FakeOrchestrator("   \n\t  ")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "hello"))

    assert client.sent == [(SENDER, PROCESSING_FAILURE_REPLY)]


def test_empty_model_response_text_is_a_processing_failure():
    orchestrator = FakeOrchestrator(_model_response("   "))
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "hello"))

    assert client.sent == [(SENDER, PROCESSING_FAILURE_REPLY)]


def test_none_response_is_a_processing_failure():
    orchestrator = FakeOrchestrator(None)
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "hello"))

    assert client.sent == [(SENDER, PROCESSING_FAILURE_REPLY)]


def test_unsupported_return_type_is_a_processing_failure():
    orchestrator = FakeOrchestrator(12345)  # neither str nor ModelResponse
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "hello"))

    assert client.sent == [(SENDER, PROCESSING_FAILURE_REPLY)]


# --- Exception privacy (Orchestrator/provider failures) -----------------


def test_orchestrator_exception_triggers_exactly_one_fixed_reply():
    orchestrator = FakeOrchestrator(RuntimeError("boom"))
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "hello"))

    assert client.sent == [(SENDER, PROCESSING_FAILURE_REPLY)]


def test_orchestrator_exception_is_logged_only_as_a_generic_category(caplog):
    orchestrator = FakeOrchestrator(RuntimeError("boom"))
    handler = MessageHandler(orchestrator, RecordingClient())

    with caplog.at_level(logging.DEBUG):
        handler.handle_task(TextTask(SENDER, "hello"))

    messages = [record.getMessage() for record in caplog.records]
    assert "processing_error" in messages
    for message in messages:
        assert "boom" not in message
        assert "Traceback" not in message


def test_synthetic_exception_with_sensitive_content_never_appears_in_logs(caplog):
    poisoned_message = (
        "sender=15551234567 text='very secret user text' "
        "token=fake-secret-token-abc123 response='fake personal response text'"
    )
    orchestrator = FakeOrchestrator(RuntimeError(poisoned_message))
    handler = MessageHandler(orchestrator, RecordingClient())

    with caplog.at_level(logging.DEBUG):
        handler.handle_task(TextTask(SENDER, "hello"))

    all_messages = "\n".join(record.getMessage() for record in caplog.records)

    assert "15551234567" not in all_messages
    assert "very secret user text" not in all_messages
    assert "fake-secret-token-abc123" not in all_messages
    assert "fake personal response text" not in all_messages


def test_outbound_failure_after_processing_error_is_logged_as_a_generic_category(caplog):
    orchestrator = FakeOrchestrator(RuntimeError("boom"))
    handler = MessageHandler(orchestrator, FailingClient())

    with caplog.at_level(logging.DEBUG):
        handler.handle_task(TextTask(SENDER, "hello"))  # should not raise

    messages = [record.getMessage() for record in caplog.records]
    assert "processing_error" in messages
    assert "outbound_failure" in messages


def test_outbound_failure_alone_is_logged_as_a_generic_category(caplog):
    handler = MessageHandler(FakeOrchestrator("a fine reply"), FailingClient())

    with caplog.at_level(logging.DEBUG):
        handler.handle_task(TextTask(SENDER, "hello"))

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["outbound_failure"]
