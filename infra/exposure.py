"""Port assignments and their exposure class. The single Python source of truth.

The demo ingress toggle (Phase A2's `var.demo_cidrs`) exists so a grader can reach the live
system for a window. The premortem (H12) found that the same toggle would have exposed the
reviewer console -- a direct write path to the graded metric behind one shared secret. The
reviewer UI therefore gets a port that no demo-exposed rule may ever carry, and
`tests/unit/test_exposure_contract.py` holds the Terraform to it.

Two boundaries this module deliberately does not cross.

* It declares no Terraform. Phase A2's `infra/terraform/` is the single root module and the
  single declaration of every security group and every CIDR variable; a second declaration
  of one address is a `terraform validate` failure, and `terraform validate` is a required
  CI check. This module is the contract A2 is measured against, not a second copy of it.
* `tf_local` names the key each port has in A2's `locals.ports`. The two vocabularies
  differ -- A2 says `backend` and `frontend` where this file says `backend_api` and
  `user_ui` -- and writing the mapping down is what lets a test compare them instead of
  quietly comparing two dictionaries that were never going to be equal.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Port:
    number: int
    service: str
    demo_exposed: bool
    tf_local: str
    note: str


PORTS: dict[str, Port] = {
    "backend_api": Port(
        8000, "fastapi", True, "backend", "Graded: /predict and /health, README curl examples"
    ),
    "user_ui": Port(8501, "streamlit-user", True, "frontend", "Graded: rubric 3.1 frontend"),
    "monitoring": Port(
        8502, "streamlit-monitoring", True, "monitoring", "Graded: rubric 3.2 dashboard"
    ),
    "reviewer_ui": Port(
        8503,
        "streamlit-reviewer",
        False,
        "reviewer_ui",
        "Operator only, reached over an SSM port forward. Premortem H12: it writes the "
        "graded metric, so no ingress rule of any kind may carry it.",
    ),
}

DEMO_EXPOSED_PORTS: frozenset[int] = frozenset(
    port.number for port in PORTS.values() if port.demo_exposed
)
OPERATOR_ONLY_PORTS: frozenset[int] = frozenset(
    port.number for port in PORTS.values() if not port.demo_exposed
)
