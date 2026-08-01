"""No module in this repository may render into a markup-capable sink.

`frontend/render.py` keeps the comment byte-identical to the string the classifier scored,
which means it cannot escape the payload; inertness has to come from the sink. So the sink
set is the control, and this file enforces it across every Python module that could reach a
browser.

The scan is an AST walk rather than a substring search, for two reasons that both bite in
practice. A substring search cannot tell a call from a docstring, so the module that
documents the rule becomes the only file that fails it -- which is how the rule ends up
deleted. And a substring search misses the smuggling forms: `getattr(st, "markdown")(x)`
and `f(**{"unsafe_allow_html": True})` both defeat it.

`test_the_scanner_actually_flags_a_planted_violation` is the load-bearing test here: a scan
that reports nothing is indistinguishable from a scan that cannot report anything.
"""

import ast
from pathlib import Path

import pytest

SCANNED_DIRS = ("frontend", "monitoring", "backend", "rescorer", "scripts")

# Streamlit sinks that parse markdown, plus the two escape hatches that render raw markup.
FORBIDDEN_SINKS = frozenset({"markdown", "write", "html"})
# Anything ending in one of these renders a string as markup.
FORBIDDEN_SUFFIXES = ("components.html", "components.v1.html", "to_html")
HTML_FLAG = "unsafe_allow_html"


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        root = Path(directory)
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def violations(source: str, where: str) -> list[str]:
    """Every way a string can reach markup from Python, named one at a time."""
    found: list[str] = []
    tree = ast.parse(source, filename=where)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == HTML_FLAG:
            found.append(f"{where}:{node.lineno} names {HTML_FLAG!r}")
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == HTML_FLAG:
                found.append(f"{where}:{node.lineno} passes {HTML_FLAG}=")
        name = _dotted(node.func)
        head, _, attribute = name.rpartition(".")
        if head in {"st", "streamlit"} and attribute in FORBIDDEN_SINKS:
            found.append(f"{where}:{node.lineno} calls {name}")
        if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            found.append(f"{where}:{node.lineno} calls {name}")
        if name in {"getattr", "st.__getattribute__"}:
            for argument in node.args[1:]:
                if isinstance(argument, ast.Constant) and argument.value in FORBIDDEN_SINKS:
                    found.append(f"{where}:{node.lineno} resolves {argument.value!r} dynamically")
    return found


def test_directories_under_scan_actually_exist():
    """A scan over nothing passes vacuously, which is how this control dies."""
    assert Path("frontend").is_dir()
    assert Path("backend").is_dir()
    assert len(_python_files()) >= 2


@pytest.mark.parametrize(
    "planted",
    [
        "import streamlit as st\nst.markdown(comment)\n",
        "import streamlit as st\nst.write(comment)\n",
        "import streamlit as st\nst.html(comment)\n",
        "import streamlit as st\nst.text(comment, unsafe_allow_html=True)\n",
        'import streamlit as st\nst.text(comment, **{"unsafe_allow_html": True})\n',
        'import streamlit as st\ngetattr(st, "markdown")(comment)\n',
        "import streamlit.components.v1 as components\ncomponents.html(comment)\n",
        "st.text(frame.to_html())\n",
    ],
    ids=["markdown", "write", "html", "flag", "flag-splat", "getattr", "components", "to_html"],
)
def test_the_scanner_actually_flags_a_planted_violation(planted: str):
    assert violations(planted, "planted.py"), planted


def test_the_scanner_does_not_flag_the_approved_sink():
    approved = 'import streamlit as st\nst.text(payload)\nst.dataframe(table)\n'
    assert violations(approved, "approved.py") == []


@pytest.mark.parametrize("path", _python_files(), ids=str)
def test_no_html_or_markdown_rendering_primitives(path: Path):
    found = violations(path.read_text(encoding="utf-8"), str(path))
    assert not found, (
        f"{found}. User and reviewer content is rendered verbatim through "
        "frontend.render.render_comment, whose sink does not interpret markup; markdown and "
        "HTML paths are forbidden because inputs here are adversarial by definition."
    )
