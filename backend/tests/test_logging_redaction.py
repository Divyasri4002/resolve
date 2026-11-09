"""
CORE-4 Phase 5 — credentials must not survive into log lines.

SerpApi authenticates with a key that must travel in the URL query string, and httpx logs every
request line at INFO with the full URL. During CORE-4's live run that combination put a real key
on the console. The fix is a logging filter that scrubs credential-shaped query values before any
handler renders the record; these tests pin both the redaction itself and the fact that the app
installs it on the loggers that emit request lines.

Hermetic like the rest of the suite: no network, no provider, no real credential — every key-shaped
value here is a fake one.
"""
import logging

from app.logging_redaction import CredentialQueryRedactionFilter, install_credential_query_redaction

SERPAPI_SHAPE = (
    'HTTP Request: GET https://serpapi.com/search?engine=google_shopping'
    '&q=anti-hairfall%20shampoo&hl=en&gl=in&api_key=FAKEKEYVALUE123456 "HTTP/1.1 200 OK"'
)


def _record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, message, args, None
    )


def test_a_serpapi_shaped_request_line_loses_its_key_and_nothing_else():
    redaction = CredentialQueryRedactionFilter()

    assert redaction.filter(_record(SERPAPI_SHAPE)) is True

    rendered = _record(SERPAPI_SHAPE)
    redaction.filter(rendered)
    text = rendered.getMessage()
    assert "FAKEKEYVALUE123456" not in text
    assert "api_key=[redacted]" in text
    # The query's non-credential parts survive — the line stays diagnosable.
    assert "engine=google_shopping" in text
    assert "q=anti-hairfall%20shampoo" in text
    assert "hl=en" in text


def test_credential_shaped_query_names_are_redacted_wherever_they_appear():
    redaction = CredentialQueryRedactionFilter()
    for query in (
        "https://example.test/x?key=AIzaFAKE9value",
        "https://example.test/x?a=1&token=bearerFAKE&b=2",
        "https://example.test/x?SECRET=hushhush",
        "https://example.test/x?PASSWORD=letmein123",
        "https://example.test/x?ACCESS_TOKEN=tok_FAKE",
    ):
        record = _record('HTTP Request: GET %s "HTTP/1.1 200 OK"', query)
        redaction.filter(record)
        rendered = record.getMessage()
        for fake in ("AIzaFAKE9value", "bearerFAKE", "hushhush", "letmein123", "tok_FAKE"):
            assert fake not in rendered, query


def test_a_line_without_credentials_is_left_exactly_as_it_was():
    redaction = CredentialQueryRedactionFilter()
    line = 'HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent "HTTP/1.1 200 OK"'
    record = _record(line)
    original_msg = record.msg
    original_args = record.args

    redaction.filter(record)

    assert record.msg == original_msg
    assert record.args == original_args
    assert record.getMessage() == line


def test_the_httpx_percent_style_record_is_redacted_after_interpolation():
    # httpx logs with a template and args; the filter must render and then scrub.
    redaction = CredentialQueryRedactionFilter()
    record = _record(
        'HTTP Request: %s %s "%s"',
        "GET",
        "https://serpapi.com/search?api_key=FAKEKEYVALUE123456&q=shampoo",
        "HTTP/1.1 200 OK",
    )

    redaction.filter(record)

    text = record.getMessage()
    assert "FAKEKEYVALUE123456" not in text
    assert "api_key=[redacted]" in text
    assert record.args is None


def test_a_record_that_cannot_be_rendered_is_passed_through_untouched():
    class _Explosive:
        def __str__(self) -> str:
            raise RuntimeError("no rendering today")

    redaction = CredentialQueryRedactionFilter()
    record = _record("broken %s record", _Explosive())

    assert redaction.filter(record) is True


def test_the_app_installs_the_filter_on_the_request_line_loggers():
    install_credential_query_redaction()
    for logger_name in ("httpx", "httpcore"):
        filters = logging.getLogger(logger_name).filters
        assert any(isinstance(entry, CredentialQueryRedactionFilter) for entry in filters), logger_name


def test_an_emitted_httpx_record_reaches_the_handler_already_scrubbed(caplog):
    install_credential_query_redaction()
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(SERPAPI_SHAPE)

    assert "FAKEKEYVALUE123456" not in caplog.text
    assert "api_key=[redacted]" in caplog.text
