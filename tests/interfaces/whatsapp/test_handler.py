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
from interfaces.whatsapp.task_control import (
    TASK_HELP_TEXT,
    ConfirmationDecision,
    TaskConfirmationWork,
    TaskExecutionWork,
    TaskRequestText,
    _GENERIC_INVALID_CONFIRMATION_TEXT,
)

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
    precisely, rather than assumed. Also records the context kwarg every
    call received, so Milestone 33's trusted-context wiring can be tested."""

    def __init__(self, result="a reply"):
        self._result = result
        self.received_prompts: list = []
        self.received_contexts: list = []

    def handle(self, prompt: str, context=None):
        self.received_prompts.append(prompt)
        self.received_contexts.append(context)
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


# --- classify_message(): Milestone 46 P1 "/task ..." recognition ------


def test_task_natural_language_request_produces_task_request_text():
    task = classify_message(_message(text="/task open notepad"))

    assert isinstance(task, TaskRequestText)
    assert task.request_text == "open notepad"
    # Never a TextTask - the orchestrator must never see this text.
    assert not isinstance(task, TextTask)


def test_bare_task_produces_fixed_help_reply():
    task = classify_message(_message(text="/task"))

    assert isinstance(task, FixedReplyTask)
    assert task.sender == SENDER
    assert task.reply_text == TASK_HELP_TEXT


def test_task_help_produces_fixed_help_reply():
    task = classify_message(_message(text="/task help"))

    assert isinstance(task, FixedReplyTask)
    assert task.reply_text == TASK_HELP_TEXT


def test_legacy_task_confirm_produces_migration_reply_not_task_request():
    task = classify_message(_message(text="/task confirm"))

    assert isinstance(task, FixedReplyTask)
    assert "CONFIRM" in task.reply_text
    assert task.reply_text != TASK_HELP_TEXT


def test_legacy_task_cancel_produces_migration_reply_not_task_request():
    task = classify_message(_message(text="/task cancel"))

    assert isinstance(task, FixedReplyTask)
    assert "REJECT" in task.reply_text


def test_oversized_task_request_is_rejected_before_task_creation():
    task = classify_message(_message(text="/task " + "x" * (MAX_INCOMING_TEXT_LENGTH)))

    # The whole message (including "/task ") exceeds max_incoming_text_length
    # - the ordinary whole-message oversized check fires first, exactly as
    # for any other message.
    assert isinstance(task, FixedReplyTask)
    assert task.reply_text == OVERSIZED_MESSAGE_REPLY


def test_task_prefix_mid_sentence_is_not_a_task_command():
    # Anchored to the start of the message - "/task" appearing mid-sentence
    # must never be treated as a command.
    task = classify_message(_message(text="please run /task help me"))

    assert isinstance(task, TextTask)
    assert task.text == "please run /task help me"


def test_knowledge_command_is_still_unaffected_by_task_routing():
    task = classify_message(_message(text="/knowledge status"))

    assert isinstance(task, TextTask)


# --- classify_message(): Milestone 46 P2B "CONFIRM/REJECT" recognition -


def test_confirm_with_token_produces_task_confirmation_work():
    task = classify_message(_message(text="CONFIRM abc-123"))

    assert isinstance(task, TaskConfirmationWork)
    assert task.confirmation_id == "abc-123"
    assert task.decision is ConfirmationDecision.CONFIRM
    assert not isinstance(task, TextTask)


def test_reject_with_token_produces_task_confirmation_work():
    task = classify_message(_message(text="REJECT abc-123"))

    assert isinstance(task, TaskConfirmationWork)
    assert task.confirmation_id == "abc-123"
    assert task.decision is ConfirmationDecision.REJECT


def test_confirm_verb_is_case_insensitive_token_case_preserved():
    task = classify_message(_message(text="confirm AbC-123"))

    assert isinstance(task, TaskConfirmationWork)
    assert task.confirmation_id == "AbC-123"  # not case-folded


def test_bare_confirm_produces_fixed_generic_invalid_reply():
    task = classify_message(_message(text="CONFIRM"))

    assert isinstance(task, FixedReplyTask)
    assert task.sender == SENDER
    assert task.reply_text == _GENERIC_INVALID_CONFIRMATION_TEXT


def test_confirm_with_extra_tokens_produces_fixed_generic_invalid_reply():
    task = classify_message(_message(text="CONFIRM abc extra"))

    assert isinstance(task, FixedReplyTask)
    assert task.reply_text == _GENERIC_INVALID_CONFIRMATION_TEXT


def test_confirm_glued_to_token_is_not_a_confirmation_command():
    # No whitespace separator - never mistaken for a command shape.
    task = classify_message(_message(text="CONFIRMabc"))

    assert isinstance(task, TextTask)
    assert task.text == "CONFIRMabc"


def test_confirmation_prefix_mid_sentence_is_not_a_confirmation_command():
    task = classify_message(_message(text="please confirm this"))

    assert isinstance(task, TextTask)


def test_oversized_confirmation_command_is_rejected_before_parsing():
    task = classify_message(_message(text="CONFIRM " + "x" * MAX_INCOMING_TEXT_LENGTH))

    # The ordinary whole-message oversized check fires first, exactly like
    # any other message - never reaches confirmation parsing at all.
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


def test_text_task_is_handled_with_a_trusted_computer_actions_context():
    # Milestone 33: this interface is the one place that grants
    # allow_computer_actions=True, and only for an already-authorized,
    # already-classified TextTask - see handler.py's _TRUSTED_CONTEXT.
    orchestrator = FakeOrchestrator("ok")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(TextTask(SENDER, "hello"))

    assert len(orchestrator.received_contexts) == 1
    context = orchestrator.received_contexts[0]
    assert context.allow_computer_actions is True
    assert context.actor == "whatsapp"


def test_knowledge_command_text_task_also_receives_the_trusted_context():
    # Milestone 37: classify_message()/MessageHandler are fully generic -
    # a "/knowledge ..." message is just another TextTask, so it reaches
    # the same _TRUSTED_CONTEXT any other text does. No knowledge-specific
    # wiring exists (or is needed) in this module.
    orchestrator = FakeOrchestrator("status: ok")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    message = IncomingMessage(
        message_id="wamid.2",
        sender=SENDER,
        phone_number_id=PHONE_NUMBER_ID,
        message_type="text",
        text="/knowledge status",
    )
    task = classify_message(message)
    handler.handle_task(task)

    assert isinstance(task, TextTask)
    assert orchestrator.received_prompts == ["/knowledge status"]
    context = orchestrator.received_contexts[0]
    assert context.allow_computer_actions is True
    assert context.actor == "whatsapp"


def test_fixed_reply_task_never_calls_orchestrator_so_no_context_is_built():
    orchestrator = FakeOrchestrator("should not be reached")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)

    handler.handle_task(FixedReplyTask(SENDER, UNSUPPORTED_MESSAGE_REPLY))

    assert orchestrator.received_contexts == []


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


# --- TaskExecutionWork: Milestone 46 P1's only execution boundary ------


def test_task_execution_work_dispatches_task_work_with_the_wired_dependencies(monkeypatch):
    import interfaces.whatsapp.handler as handler_module

    calls = []

    def fake_dispatch_task_work(
        repository, task_id, catalog, planner_provider, registry,
        tools_config_loader, respond_provider, client, authorized_sender,
    ):
        calls.append((
            repository, task_id, catalog, planner_provider, registry,
            tools_config_loader, respond_provider, client, authorized_sender,
        ))

    monkeypatch.setattr(handler_module, "dispatch_task_work", fake_dispatch_task_work)

    (
        sentinel_repo, sentinel_catalog, sentinel_provider, sentinel_registry,
        sentinel_loader, sentinel_respond_provider, sentinel_sender,
    ) = (object(), object(), object(), object(), object(), object(), object())
    sentinel_client = RecordingClient()
    handler = MessageHandler(
        FakeOrchestrator("should not be reached"),
        sentinel_client,
        task_repository=sentinel_repo,
        task_catalog=sentinel_catalog,
        planner_provider=sentinel_provider,
        action_registry=sentinel_registry,
        tools_config_loader=sentinel_loader,
        respond_provider=sentinel_respond_provider,
        authorized_sender=sentinel_sender,
    )

    handler.handle_task(TaskExecutionWork(task_id="task-123"))

    assert calls == [(
        sentinel_repo, "task-123", sentinel_catalog, sentinel_provider, sentinel_registry,
        sentinel_loader, sentinel_respond_provider, sentinel_client, sentinel_sender,
    )]


def test_task_execution_work_message_handler_adds_no_side_effects_of_its_own(monkeypatch):
    # MessageHandler._handle_task_execution_work() is a thin wrapper - with
    # dispatch_task_work() itself neutralized, MessageHandler must not
    # independently call the orchestrator or send anything.
    import interfaces.whatsapp.handler as handler_module

    monkeypatch.setattr(handler_module, "dispatch_task_work", lambda *a, **k: None)

    orchestrator = FakeOrchestrator("should not be reached")
    client = RecordingClient()
    handler = MessageHandler(
        orchestrator,
        client,
        task_repository=object(),
        task_catalog=object(),
        planner_provider=object(),
        action_registry=object(),
        tools_config_loader=object(),
        respond_provider=object(),
        authorized_sender=object(),
    )

    handler.handle_task(TaskExecutionWork(task_id="task-123"))

    # Milestone 46 P1 sends no outbound WhatsApp message of its own for a
    # TaskExecutionWork - no "Task accepted" notice, no result, no
    # confirmation (all P2).
    assert client.sent == []
    assert orchestrator.received_prompts == []


# --- TaskConfirmationWork: Milestone 46 P2B's only confirmation boundary --


def test_task_confirmation_work_dispatches_confirmation_work_with_the_wired_dependencies(monkeypatch):
    import interfaces.whatsapp.handler as handler_module

    calls = []

    def fake_dispatch_confirmation_work(
        repository, work, registry, tools_config_loader, respond_provider, client, authorized_sender,
    ):
        calls.append((repository, work, registry, tools_config_loader, respond_provider, client, authorized_sender))

    monkeypatch.setattr(handler_module, "dispatch_confirmation_work", fake_dispatch_confirmation_work)

    (
        sentinel_repo, sentinel_registry, sentinel_loader, sentinel_respond_provider, sentinel_sender,
    ) = (object(), object(), object(), object(), object())
    sentinel_client = RecordingClient()
    handler = MessageHandler(
        FakeOrchestrator("should not be reached"),
        sentinel_client,
        task_repository=sentinel_repo,
        action_registry=sentinel_registry,
        tools_config_loader=sentinel_loader,
        respond_provider=sentinel_respond_provider,
        authorized_sender=sentinel_sender,
    )

    work = TaskConfirmationWork(confirmation_id="abc-123", decision=ConfirmationDecision.CONFIRM)
    handler.handle_task(work)

    assert calls == [(
        sentinel_repo, work, sentinel_registry, sentinel_loader, sentinel_respond_provider,
        sentinel_client, sentinel_sender,
    )]


def test_task_confirmation_work_message_handler_adds_no_side_effects_of_its_own(monkeypatch):
    # MessageHandler._handle_confirmation_work() is a thin wrapper - with
    # dispatch_confirmation_work() itself neutralized, MessageHandler must
    # not independently call the orchestrator or send anything.
    import interfaces.whatsapp.handler as handler_module

    monkeypatch.setattr(handler_module, "dispatch_confirmation_work", lambda *a, **k: None)

    orchestrator = FakeOrchestrator("should not be reached")
    client = RecordingClient()
    handler = MessageHandler(
        orchestrator,
        client,
        task_repository=object(),
        action_registry=object(),
        tools_config_loader=object(),
        respond_provider=object(),
        authorized_sender=object(),
    )

    handler.handle_task(TaskConfirmationWork(confirmation_id="abc-123", decision=ConfirmationDecision.REJECT))

    assert client.sent == []
    assert orchestrator.received_prompts == []


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
