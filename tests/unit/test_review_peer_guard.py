"""The reviewer routes are mounted on the same app as /predict, which demo_cidrs opens to
0.0.0.0/0. A security group rule is per-port; an application is per-path, so the port cannot
tell the moderation queue from the prediction endpoint.

Every legitimate caller is inside the VPC -- roll.sh points the console at the backend's
private address -- so a public peer on /review/* is by definition not the console.

404 rather than 403: the response should not confirm the route exists.

This does NOT remove the shared secret and does not pretend to. The public UI container
shares the frontend instance's security group, so it reaches these routes from a private
address; the secret is still the only control on that path.

The four "public" addresses below are not this project's own Elastic IPs -- `scripts/redact.py`
treats any globally routable address that is not one of the three published endpoints as
something to mask, and `docs/superpowers/plans/2026-08-11-review-exposure-and-graded-panels.md`
carries the masked form `<elastic-ip>` in this parametrize list as a result. What the test
needs is only that `ipaddress.ip_address(host).is_global` is True, so well-known public
resolvers stand in: none of them is infrastructure this project owns.
"""

import pytest

from backend.app import REVIEWER_PATH_PREFIX, peer_is_public


@pytest.mark.parametrize(
    "host",
    ["8.8.8.8", "1.1.1.1", "9.9.9.9", "2001:4860:4860::8888"],
)
def test_a_routable_address_is_public(host):
    assert peer_is_public(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "10.42.0.173",       # the backend's private address, from /toxic/endpoints/backend-internal
        "10.0.1.55",
        "172.17.0.4",        # docker bridge
        "192.168.1.10",
        "127.0.0.1",
        "::1",
    ],
)
def test_a_private_or_loopback_address_is_not_public(host):
    assert peer_is_public(host) is False


def test_a_peer_that_is_not_an_address_is_not_public():
    """Starlette's TestClient reports 'testclient'. A non-address peer means there is no TCP
    peer at all, which in this deployment is only ever an in-process caller. Treating it as
    public would fail every existing reviewer test for a reason unrelated to the control."""
    assert peer_is_public("testclient") is False
    assert peer_is_public(None) is False
    assert peer_is_public("") is False


def test_the_prefix_covers_login_as_well_as_the_read_and_write_routes():
    for path in ("/review/login", "/review/pending", "/review/submit"):
        assert path.startswith(REVIEWER_PATH_PREFIX)


def test_the_prefix_does_not_cover_the_graded_anonymous_feedback_route():
    """rubric 3.2 grades /feedback/user, and the user UI calls it over the internet."""
    assert not "/feedback/user".startswith(REVIEWER_PATH_PREFIX)
    assert not "/predict".startswith(REVIEWER_PATH_PREFIX)
