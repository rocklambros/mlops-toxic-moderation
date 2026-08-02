"""The Terraform reader, exercised against the inputs that break the naive version.

A parser nobody tested is a parser that returns `{}` on the file that matters, and every
assertion written over `{}` passes. Each case below is a shape that actually occurs in
`infra/terraform/`.
"""

from __future__ import annotations

import pytest

from tests.infra import tfparse


def test_a_comment_containing_hcl_does_not_become_hcl():
    text = '# resource "aws_s3_bucket" "ghost" {\nforce_destroy = true\n'
    assert 'resource' not in tfparse.strip_noise(text)
    assert "force_destroy = true" in tfparse.strip_noise(text)


def test_a_hash_inside_a_string_is_not_a_comment():
    text = 'bucket = "toxic#deploy"\nforce_destroy = true\n'
    assert "toxic#deploy" in tfparse.strip_noise(text)
    assert "force_destroy" in tfparse.strip_noise(text)


def test_a_heredoc_body_is_not_parsed_as_configuration():
    text = 'description = <<-EOT\n  resource "aws_s3_bucket" "ghost" { }\nEOT\nvalue = 1\n'
    stripped = tfparse.strip_noise(text)
    assert "ghost" not in stripped
    assert "value = 1" in stripped


def test_an_interpolated_string_does_not_end_the_block_early():
    """`"${var.project}-deploy-${local.account_id}"` carries four braces."""
    body = tfparse._balanced_body(
        '{\n  bucket = "${var.project}-deploy-${local.account_id}"\n  force_destroy = true\n}', 0
    )
    attributes = tfparse._attributes(body)
    assert attributes["force_destroy"] is True
    assert attributes["bucket"] == "${var.project}-deploy-${local.account_id}"


def test_booleans_and_numbers_are_typed_not_strings():
    attributes = tfparse._attributes("a = true\nb = false\nc = 42\nd = \"true\"\n")
    assert attributes["a"] is True
    assert attributes["b"] is False
    assert attributes["c"] == 42
    assert attributes["d"] == "true"


def test_a_nested_block_does_not_leak_its_arguments_into_the_parent():
    body = (
        'name = "outer"\n'
        'tags {\n  name = "inner"\n}\n'
        'versioning_configuration {\n  status = "Enabled"\n}\n'
    )
    attributes = tfparse._attributes(body)
    assert attributes["name"] == "outer"
    assert "status" not in attributes


def test_a_map_or_list_argument_is_captured_whole():
    attributes = tfparse._attributes('for_each = {\n  a = 1\n  b = 2\n}\nname = "x"\n')
    assert "a = 1" in str(attributes["for_each"])
    assert attributes["name"] == "x"


def test_unbalanced_braces_raise_rather_than_returning_a_truncated_block():
    with pytest.raises(ValueError):
        tfparse._balanced_body("{ a = 1", 0)


def test_the_real_module_parses_and_finds_resources_that_are_known_to_exist():
    """The scan must reach the actual files; an empty result certifies nothing."""
    assert "trail" in tfparse.resource_names("aws_s3_bucket")
    assert tfparse.resource_names("aws_instance") == {"backend", "frontend", "monitoring"}
    assert tfparse.resource_names("aws_eip") == {"backend", "frontend", "monitoring"}
    trail = tfparse.resources_of_kind("aws_s3_bucket")["trail"]
    assert trail["force_destroy"] is True
