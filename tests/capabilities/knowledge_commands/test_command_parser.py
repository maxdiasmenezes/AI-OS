"""Tests for capabilities/knowledge_commands/command_parser.py."""

from capabilities.knowledge_commands.command_parser import (
    KnowledgeParseError,
    ParsedKnowledgeCommand,
    parse_knowledge_command,
)


# --- bare prefix / help --------------------------------------------------


def test_bare_prefix_is_help():
    assert parse_knowledge_command("/knowledge") == ParsedKnowledgeCommand("help")


def test_explicit_help():
    assert parse_knowledge_command("/knowledge help") == ParsedKnowledgeCommand("help")


def test_prefix_and_verb_are_case_insensitive():
    assert parse_knowledge_command("/Knowledge STATUS") == ParsedKnowledgeCommand("status")
    assert parse_knowledge_command("/KNOWLEDGE help") == ParsedKnowledgeCommand("help")


def test_leading_trailing_whitespace_tolerated():
    assert parse_knowledge_command("   /knowledge status   ") == ParsedKnowledgeCommand("status")


def test_not_a_knowledge_command():
    result = parse_knowledge_command("hello there")
    assert isinstance(result, KnowledgeParseError)


def test_similar_but_different_prefix_does_not_match():
    result = parse_knowledge_command("/knowledgeable status")
    assert isinstance(result, KnowledgeParseError)


# --- status ----------------------------------------------------------------


def test_valid_status_no_args():
    assert parse_knowledge_command("/knowledge status") == ParsedKnowledgeCommand("status")


def test_valid_status_with_source():
    result = parse_knowledge_command("/knowledge status --source ai_os_docs")
    assert result == ParsedKnowledgeCommand("status", source_key="ai_os_docs")


def test_status_source_option_is_case_insensitive():
    result = parse_knowledge_command("/knowledge status --SOURCE ai_os_docs")
    assert result == ParsedKnowledgeCommand("status", source_key="ai_os_docs")


def test_status_source_key_is_casefolded():
    result = parse_knowledge_command("/knowledge status --source AI_OS_DOCS")
    assert result == ParsedKnowledgeCommand("status", source_key="ai_os_docs")


def test_unexpected_status_argument():
    result = parse_knowledge_command("/knowledge status extra")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "unexpected_argument"


def test_status_missing_source_value():
    result = parse_knowledge_command("/knowledge status --source")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "missing_option_value"


def test_status_duplicate_source_option():
    result = parse_knowledge_command("/knowledge status --source a --source b")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "duplicate_option"


def test_status_unknown_option():
    result = parse_knowledge_command("/knowledge status --bogus a")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "unknown_option"


def test_status_path_shaped_source_rejected():
    result = parse_knowledge_command("/knowledge status --source C:/somewhere")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


def test_status_dotdot_source_rejected():
    result = parse_knowledge_command("/knowledge status --source ../../etc")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


def test_status_slash_source_rejected():
    result = parse_knowledge_command("/knowledge status --source a/b")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


def test_status_backslash_source_rejected():
    result = parse_knowledge_command("/knowledge status --source a\\b")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


# --- ingest ------------------------------------------------------------


def test_valid_ingest():
    result = parse_knowledge_command("/knowledge ingest ai_os_docs")
    assert result == ParsedKnowledgeCommand("ingest", source_key="ai_os_docs")


def test_ingest_source_key_is_casefolded():
    result = parse_knowledge_command("/knowledge ingest AI_OS_DOCS")
    assert result == ParsedKnowledgeCommand("ingest", source_key="ai_os_docs")


def test_unexpected_ingest_argument():
    result = parse_knowledge_command("/knowledge ingest ai_os_docs extra")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "wrong_argument_count"


def test_ingest_missing_argument():
    result = parse_knowledge_command("/knowledge ingest")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "wrong_argument_count"


def test_ingest_path_shaped_source_rejected():
    result = parse_knowledge_command("/knowledge ingest C:/somewhere")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


def test_ingest_dotdot_source_rejected():
    result = parse_knowledge_command("/knowledge ingest ..")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


def test_ingest_sql_shaped_source_rejected():
    # A single token (no whitespace) containing SQL-shaped punctuation
    # fails the source-key shape check.
    result = parse_knowledge_command("/knowledge ingest ';drop_table_sources;--")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


def test_ingest_sql_shaped_source_with_whitespace_fails_argument_count():
    # A multi-word SQL-shaped attempt splits into multiple positional
    # tokens before it ever reaches shape validation - still a hard
    # parse failure either way.
    result = parse_knowledge_command("/knowledge ingest ';DROP TABLE sources;--")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "wrong_argument_count"


# --- confirm / cancel -----------------------------------------------------


def test_valid_confirm():
    assert parse_knowledge_command("/knowledge confirm") == ParsedKnowledgeCommand("confirm")


def test_valid_cancel():
    assert parse_knowledge_command("/knowledge cancel") == ParsedKnowledgeCommand("cancel")


def test_unexpected_confirm_argument():
    result = parse_knowledge_command("/knowledge confirm ai_os_docs")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "wrong_argument_count"


def test_unexpected_cancel_argument():
    result = parse_knowledge_command("/knowledge cancel now")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "wrong_argument_count"


def test_unexpected_help_argument():
    result = parse_knowledge_command("/knowledge help me")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "wrong_argument_count"


# --- search: valid forms ----------------------------------------------------


def test_valid_search_with_mandatory_delimiter():
    result = parse_knowledge_command("/knowledge search -- repository backup")
    assert result == ParsedKnowledgeCommand("search", query="repository backup")


def test_valid_search_with_source():
    result = parse_knowledge_command("/knowledge search --source ai_os_docs -- repository backup")
    assert result == ParsedKnowledgeCommand(
        "search", source_key="ai_os_docs", query="repository backup"
    )


def test_valid_search_with_limit():
    result = parse_knowledge_command("/knowledge search --limit 5 -- repository backup")
    assert result == ParsedKnowledgeCommand("search", limit=5, query="repository backup")


def test_valid_search_source_then_limit():
    result = parse_knowledge_command(
        "/knowledge search --source ai_os_docs --limit 5 -- repository backup"
    )
    assert result == ParsedKnowledgeCommand(
        "search", source_key="ai_os_docs", limit=5, query="repository backup"
    )


def test_valid_search_limit_then_source():
    result = parse_knowledge_command(
        "/knowledge search --limit 5 --source ai_os_docs -- repository backup"
    )
    assert result == ParsedKnowledgeCommand(
        "search", source_key="ai_os_docs", limit=5, query="repository backup"
    )


def test_search_source_key_is_casefolded():
    result = parse_knowledge_command("/knowledge search --source AI_OS_DOCS -- backup")
    assert result.source_key == "ai_os_docs"


def test_search_option_names_are_case_insensitive():
    result = parse_knowledge_command("/knowledge search --SOURCE ai_os_docs --LIMIT 5 -- backup")
    assert result == ParsedKnowledgeCommand(
        "search", source_key="ai_os_docs", limit=5, query="backup"
    )


def test_valid_unicode_query():
    result = parse_knowledge_command("/knowledge search -- caf\u00e9 na\u00efve \u4e2d\u6587")
    assert result.query == "caf\u00e9 na\u00efve \u4e2d\u6587"


def test_query_whitespace_runs_normalized_to_single_space():
    result = parse_knowledge_command("/knowledge search --   repository    backup")
    # "--" then extra internal whitespace between query words collapses.
    assert result.query == "repository backup"


def test_query_text_preserves_punctuation():
    result = parse_knowledge_command('/knowledge search -- "quoted" text, with-punctuation!')
    assert result.query == '"quoted" text, with-punctuation!'


def test_query_text_after_delimiter_is_never_parsed_as_options():
    result = parse_knowledge_command("/knowledge search -- --source sneaky --limit 999")
    assert result.query == "--source sneaky --limit 999"
    assert result.source_key is None
    assert result.limit is None


def test_duplicate_delimiter_becomes_part_of_query_text():
    result = parse_knowledge_command("/knowledge search --source a -- first -- second")
    assert result.source_key == "a"
    assert result.query == "first -- second"


# --- search: malformed forms -----------------------------------------------


def test_search_missing_delimiter():
    result = parse_knowledge_command("/knowledge search repository backup")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "missing_delimiter"


def test_search_missing_delimiter_with_options_only():
    result = parse_knowledge_command("/knowledge search --source ai_os_docs")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "missing_delimiter"


def test_search_no_args_at_all():
    result = parse_knowledge_command("/knowledge search")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "missing_delimiter"


def test_search_blank_query():
    result = parse_knowledge_command("/knowledge search --")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "blank_query"


def test_search_blank_query_with_options():
    result = parse_knowledge_command("/knowledge search --source ai_os_docs --")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "blank_query"


def test_search_unknown_option():
    result = parse_knowledge_command("/knowledge search --bogus x -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "unknown_option"


def test_search_duplicate_source_option():
    result = parse_knowledge_command("/knowledge search --source a --source b -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "duplicate_option"


def test_search_duplicate_limit_option():
    result = parse_knowledge_command("/knowledge search --limit 5 --limit 6 -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "duplicate_option"


def test_search_missing_source_value():
    result = parse_knowledge_command("/knowledge search --source -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "missing_option_value"


def test_search_missing_limit_value():
    result = parse_knowledge_command("/knowledge search --limit -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "missing_option_value"


def test_search_non_integer_limit():
    result = parse_knowledge_command("/knowledge search --limit abc -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_limit"


def test_search_float_limit_rejected():
    result = parse_knowledge_command("/knowledge search --limit 3.5 -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_limit"


def test_search_zero_limit_rejected():
    result = parse_knowledge_command("/knowledge search --limit 0 -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_limit"


def test_search_negative_limit_rejected():
    result = parse_knowledge_command("/knowledge search --limit -1 -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_limit"


def test_search_limit_above_ten_rejected():
    result = parse_knowledge_command("/knowledge search --limit 11 -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_limit"


def test_search_limit_of_exactly_ten_is_valid():
    result = parse_knowledge_command("/knowledge search --limit 10 -- query")
    assert result.limit == 10


def test_search_limit_of_exactly_one_is_valid():
    result = parse_knowledge_command("/knowledge search --limit 1 -- query")
    assert result.limit == 1


def test_search_path_shaped_source_rejected():
    result = parse_knowledge_command("/knowledge search --source C:/somewhere -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


def test_search_dotdot_source_rejected():
    result = parse_knowledge_command("/knowledge search --source .. -- query")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "invalid_source_key"


def test_search_sql_shaped_source_option_rejected():
    result = parse_knowledge_command("/knowledge search --source \"'; DROP TABLE\" -- query")
    assert isinstance(result, KnowledgeParseError)


def test_search_fts_shaped_source_option_is_treated_as_a_plain_literal():
    # An FTS-keyword-shaped value (NEAR/OR/AND/etc.) is plain alphabetic
    # text, so it passes the parser's shape check like any other word -
    # the parser never special-cases FTS syntax. The real protection is
    # downstream: it must still match the approved-sources allowlist (see
    # capability-level tests), and even then it is only ever used as a
    # bound source_key parameter, never embedded in an FTS MATCH
    # expression.
    result = parse_knowledge_command("/knowledge search --source NEAR -- query")
    assert result == ParsedKnowledgeCommand("search", source_key="near", query="query")


def test_search_fts_shaped_query_text_is_never_parsed_specially():
    # FTS operator-shaped text appearing as the *query* (after --) must
    # also never be treated as anything but literal text by the parser.
    result = parse_knowledge_command('/knowledge search -- "quoted" OR NEAR/2 title:term')
    assert result.query == '"quoted" OR NEAR/2 title:term'


# --- unknown verb ------------------------------------------------------


def test_unknown_verb():
    result = parse_knowledge_command("/knowledge frobnicate")
    assert isinstance(result, KnowledgeParseError)
    assert result.reason == "unknown_verb"
