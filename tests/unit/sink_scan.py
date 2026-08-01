"""Shared AST scan: which Streamlit widgets get handed values the module did not choose.

Both UIs render adversarial text. `frontend.render.render_comment` is the one sink that
does not parse markdown, and it is only the one sink if nothing else on the page is fed a
value that came off the wire. So the rule is: a markdown-capable widget receives a string
literal, a module-level string constant, or the output of a formatter that has its own test
proving it cannot carry caller-supplied text.

Kept out of the test modules themselves so the two UIs are measured by the same scanner
rather than by two that drift.
"""

import ast

MARKDOWN_SINKS = frozenset(
    {
        "error",
        "warning",
        "info",
        "success",
        "caption",
        "title",
        "header",
        "subheader",
        "metric",
    }
)


def _fixed_strings(tree: ast.Module) -> set[str]:
    """Module-level string constants are literals under another name."""
    return {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def interpolated_sink_calls(source: str, vetted: frozenset[str] = frozenset()) -> list[str]:
    tree = ast.parse(source)
    fixed = _fixed_strings(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in MARKDOWN_SINKS:
            continue
        for argument in list(node.args) + [keyword.value for keyword in node.keywords]:
            if isinstance(argument, ast.Constant):
                continue
            if isinstance(argument, ast.Name) and argument.id in fixed:
                continue
            if (
                isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Name)
                and argument.func.id in vetted
            ):
                continue
            offenders.append(f"line {node.lineno}: st.{node.func.attr}(...)")
    return offenders
