"""Read the Terraform root module as data, so a test can assert on structure not on text.

`"force_destroy = true" in source` is the assertion this module exists to replace. It passes
on a commented-out line, on a different resource's argument, and on a `false` that a merge
put back -- and it fails on a reformat. What a test actually wants to say is "the resource
named `deploy` of kind `aws_s3_bucket` sets `force_destroy` to boolean true", which needs a
parser.

Deliberately a small one. HCL2 has a real grammar and this reads a subset of it: top-level
`resource "<kind>" "<name>" { ... }` blocks and the scalar arguments directly inside them.
Nested blocks are skipped rather than misparsed, and any value that is not an obvious
literal is returned as its raw source text -- an interpolation is a fact about the
configuration, and pretending to evaluate it would be worse than handing it over.

Three details are the whole reason this is a scanner rather than a regex:

* **Strings are copied verbatim.** `"${var.project}-deploy-${local.account_id}"` carries two
  `{` and two `}`. Counting braces without knowing where strings are ends the block early
  and the resource loses half its arguments.
* **Comments are removed before anything else.** Every file in this module is more comment
  than code, and several of those comments contain example HCL.
* **Heredocs are consumed whole.** `variables.tf` uses `<<-EOT` for descriptions, and the
  text inside them contains braces and quotes that are not HCL at all.

`tests/unit/test_tfparse.py` exercises each of those against inputs that break the naive
version, because a parser nobody tested is a parser that silently reports an empty dict --
and an assertion over an empty dict passes.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform"

_HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\r?\n")


def strip_noise(text: str) -> str:
    """Return `text` with comments removed and heredoc bodies blanked.

    String literals survive untouched: a `#` inside one is a character, not a comment.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            out.append(char)
            index += 1
            while index < length:
                if text[index] == "\\":
                    out.append(text[index : index + 2])
                    index += 2
                    continue
                out.append(text[index])
                index += 1
                if text[index - 1] == '"':
                    break
            continue
        if char == "#" or text[index : index + 2] == "//":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if text[index : index + 2] == "/*":
            close = text.find("*/", index)
            index = length if close < 0 else close + 2
            continue
        heredoc = _HEREDOC.match(text, index)
        if heredoc:
            terminator = re.compile(rf"(?m)^[ \t]*{heredoc.group(1)}[ \t]*$")
            end = terminator.search(text, heredoc.end())
            out.append('""')  # a heredoc is a string; leave a string in its place
            index = length if end is None else end.end()
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _balanced_body(text: str, open_brace: int) -> str:
    """The source between `open_brace` and its matching close, exclusive."""
    depth = 0
    index = open_brace
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            index += 1
            while index < length and text[index] != '"':
                index += 2 if text[index] == "\\" else 1
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : index]
        index += 1
    raise ValueError("unbalanced braces in the Terraform source")


def _scalar(raw: str) -> object:
    value = raw.strip()
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r'"[^"]*"', value):
        return value[1:-1]
    return value


def _attributes(body: str) -> dict[str, object]:
    """Scalar `key = value` pairs directly inside `body`. Nested blocks are skipped."""
    found: dict[str, object] = {}
    index = 0
    length = len(body)
    while index < length:
        match = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(=)?\s*").match(body, index)
        if not match:
            index += 1
            continue
        name, is_assignment = match.group(1), match.group(2)
        cursor = match.end()
        if not is_assignment:
            # A nested block: `tags {`, `statement {`, `rule {`. Skip its whole body.
            if cursor < length and body[cursor] == "{":
                index = cursor + len(_balanced_body(body, cursor)) + 2
                continue
            index = match.end()
            continue
        if cursor < length and body[cursor] in "{[":
            depth, scan = 0, cursor
            while scan < length:
                if body[scan] == '"':
                    scan += 1
                    while scan < length and body[scan] != '"':
                        scan += 2 if body[scan] == "\\" else 1
                elif body[scan] in "{[":
                    depth += 1
                elif body[scan] in "}]":
                    depth -= 1
                    if depth == 0:
                        break
                scan += 1
            found[name] = body[cursor : scan + 1].strip()
            index = scan + 1
            continue
        newline = body.find("\n", cursor)
        end = length if newline < 0 else newline
        found[name] = _scalar(body[cursor:end])
        index = end
    return found


@lru_cache(maxsize=1)
def _module() -> dict[tuple[str, str], dict[str, object]]:
    parsed: dict[tuple[str, str], dict[str, object]] = {}
    files = sorted(TERRAFORM_DIR.glob("*.tf"))
    if not files:
        raise FileNotFoundError(f"no Terraform source under {TERRAFORM_DIR}")
    header = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{')
    for path in files:
        text = strip_noise(path.read_text(encoding="utf-8"))
        for match in header.finditer(text):
            body = _balanced_body(text, match.end() - 1)
            parsed[(match.group(1), match.group(2))] = _attributes(body)
    return parsed


def resources_of_kind(kind: str) -> dict[str, dict[str, object]]:
    """Every resource of `kind`, keyed on its Terraform name."""
    return {name: body for (found, name), body in _module().items() if found == kind}


def resource_names(kind: str) -> set[str]:
    return set(resources_of_kind(kind))


def source_of(filename: str) -> str:
    """The comment-free source of one file, for assertions a structure walk cannot make."""
    return strip_noise((TERRAFORM_DIR / filename).read_text(encoding="utf-8"))
