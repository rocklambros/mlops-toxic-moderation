"""The comment-rendering guarantee.

Every string this project renders is an attacker's. Two properties have to hold at once and
each one is invisible to a test of the other:

1. The payload reaches an inert sink. `st.text` neither parses markdown nor interprets
   HTML, so a stored `<img src=x onerror=...>` is displayed rather than executed. This file
   tests that by intercepting the *real* default renderer with a fake `streamlit` module
   and a live XSS payload, so a swap of `st.text` for `st.markdown` is caught even though
   the payload passed to it would still be byte-identical.
2. The payload is not transformed on the way. A renderer that eats asterisks, collapses
   whitespace or drops angle brackets means the reviewer labels a DIFFERENT string than the
   classifier scored -- attacker-controlled ground-truth poisoning that satisfies (1)
   perfectly.
"""

import sys
import types

import pytest

from frontend.render import RENDERER_NAME, render_comment

XSS = "<script>alert('pwned')</script><img src=x onerror=alert(document.cookie)>"

ADVERSARIAL = (
    "**bold**  <img src=x onerror=alert(1)>\n"
    "# heading\n"
    "  leading and trailing spaces  \n"
    "- list item\n"
    "&lt;already escaped&gt;\n"
    "|table|cell|\n"
    "`code`  ~~strike~~ \\backslash\n"
    "\u200b\u202e zero width and bidi \u202c"
)

# Streamlit primitives that parse markdown, and therefore raw HTML once anyone passes the
# html flag. The renderer must not be any of them.
MARKDOWN_SINKS = frozenset(
    {"markdown", "write", "html", "caption", "title", "header", "subheader", "success",
     "error", "warning", "info", "metric"}
)


class _FakeStreamlit(types.ModuleType):
    """Records every call made against it, so the default renderer is observed rather than
    described. Any attribute is callable, so choosing the wrong one is visible instead of
    raising an AttributeError that a test might mistake for a pass."""

    def __init__(self) -> None:
        super().__init__("streamlit")
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return _record


@pytest.fixture()
def fake_streamlit(monkeypatch):
    fake = _FakeStreamlit()
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    return fake


def test_the_default_renderer_sends_an_xss_payload_to_an_inert_sink(fake_streamlit):
    """The control this file exists for. Not 'the constant says st.text' -- the actual call
    the module makes, with an actual payload."""
    render_comment(XSS)
    assert len(fake_streamlit.calls) == 1
    name, args, kwargs = fake_streamlit.calls[0]
    assert name == "text", f"user text reached st.{name}, which is not inert"
    assert name not in MARKDOWN_SINKS
    assert args == (XSS,)
    assert kwargs == {}, "the default renderer passes no flags, so none can be an html flag"


def test_the_renderer_name_is_not_decoration(fake_streamlit):
    """RENDERER_NAME and the call must be one fact. A constant that merely claims 'st.text'
    while the code calls st.markdown is the whole failure mode."""
    render_comment("hello")
    called, _, _ = fake_streamlit.calls[0]
    assert RENDERER_NAME == f"st.{called}"


def test_default_renderer_is_a_non_markdown_streamlit_primitive():
    assert RENDERER_NAME in {"st.text", "st.code"}


def test_rendered_payload_is_byte_identical_to_the_input():
    """If this ever fails, the reviewer is labelling a different string than the
    classifier scored, and the labels are attacker-shaped."""
    calls: list[str] = []
    payload = render_comment(ADVERSARIAL, renderer=calls.append)
    assert payload == ADVERSARIAL
    assert calls == [ADVERSARIAL]


def test_an_xss_payload_survives_byte_for_byte_rather_than_being_silently_rewritten():
    """Escaping here would be the wrong fix: it would change the labelled string. The
    payload is kept intact and made harmless by the sink, not by editing the evidence."""
    calls: list[str] = []
    render_comment(XSS, renderer=calls.append)
    assert calls == [XSS]


def test_markdown_metacharacters_survive_untouched():
    calls: list[str] = []
    render_comment("**not bold** _not italic_ # not a heading", renderer=calls.append)
    assert calls[0] == "**not bold** _not italic_ # not a heading"


def test_whitespace_is_not_collapsed():
    calls: list[str] = []
    render_comment("a     b\n\n\nc", renderer=calls.append)
    assert calls[0] == "a     b\n\n\nc"


def test_render_comment_accepts_no_html_flag():
    import inspect

    params = inspect.signature(render_comment).parameters
    assert "unsafe_allow_html" not in params
    assert set(params) == {"text", "renderer"}


def test_none_and_empty_render_as_an_explicit_placeholder_not_a_crash():
    calls: list[str] = []
    render_comment("", renderer=calls.append)
    render_comment(None, renderer=calls.append)
    assert calls == ["(empty comment)", "(empty comment)"]


def test_a_non_string_payload_is_stringified_rather_than_handed_to_the_sink_raw():
    """`distilbert_probs` and other JSONB columns come back as dicts. A sink handed a
    non-string is a rendering decision made by Streamlit rather than by this module."""
    calls: list[str] = []
    render_comment(123, renderer=calls.append)
    assert calls == ["123"]
