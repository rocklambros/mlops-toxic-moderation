"""Render user-supplied comments verbatim, into an inert sink.

Two rules, and the second is the one a checklist misses.

1. The sink must not interpret HTML. Inputs here are adversarial by definition -- the corpus
   is real abuse and the live endpoint is on the internet -- so a stored payload rendered
   into markup would run in the reviewer's browser and steal the reviewer session, which is
   the write path to the graded metric. `st.text` writes the string as text: no markdown
   parsing, no markup, nothing to escape into.
2. The sink must not transform the string either. A markdown sink eats asterisks, collapses
   runs of whitespace, turns a leading '#' into a heading, and drops raw angle brackets. The
   reviewer would then be labelling a DIFFERENT string than the classifier scored, which is
   attacker-controlled ground-truth poisoning that satisfies rule 1 perfectly.

Rule 2 is why the payload is not escaped here. Escaping would make the displayed string
differ from the scored string, which is precisely the failure rule 2 names; inertness is the
sink's job, and `tests/unit/test_no_unsafe_html.py` is what keeps every other sink out of
the repository.

The renderer is injectable so the guarantee is testable without a Streamlit runtime, and
the default renderer dispatches through RENDERER_NAME so that the constant and the call
cannot drift apart -- a constant that merely claims "st.text" while the code calls something
else would satisfy a naive assertion completely.
"""

from collections.abc import Callable

RENDERER_NAME = "st.text"
EMPTY_PLACEHOLDER = "(empty comment)"


def _default_renderer(payload: str) -> None:
    import streamlit as st

    # One argument, no keywords: there is no parameter here for an html flag to travel in.
    getattr(st, RENDERER_NAME.split(".", 1)[1])(payload)


def render_comment(text: object | None, renderer: Callable[[str], None] | None = None) -> str:
    payload = str(text) if text else EMPTY_PLACEHOLDER
    (renderer or _default_renderer)(payload)
    return payload
