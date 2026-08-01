"""The UIs' only I/O. Neither Streamlit process opens a database connection.

`session_fp` is minted server-side into `st.session_state` and never reaches the browser. It
is the rate-limit bucket for UI traffic, because every UI request shares one TCP peer as far
as the backend is concerned; the backend only honours it from a caller that also presents
the frontend's API key, and only in the 16-hex shape `new_session_fp` produces.

Two error-handling decisions here are controls rather than taste.

* `BackendError` never interpolates the response body into its message. The UIs surface
  exceptions through Streamlit widgets that parse markdown, and a FastAPI 422 echoes the
  offending input straight back -- so a message built from `response.text` would be a
  second rendering path for attacker-controlled text that bypasses `frontend.render`
  entirely. The body is kept, bounded, on `.detail`, which the UI renders through the
  inert path if it shows it at all.
* The verdict vocabulary and the input cap are imported, not restated.
  `backend.feedback.USER_VERDICTS` is the one definition of the former, and
  `model.normalize.MAX_INPUT_CHARS` of the latter -- deliberately not an environment
  variable, because an abuse control a deploy can widen is not a control (delivery spec
  section 6.3).

Both of those imports are from modules that pull in nothing but the standard library, so
the UI images carry no database driver; `test_the_ui_client_imports_no_database_driver`
asserts the closure rather than trusting the file.
"""

import secrets
from dataclasses import dataclass, field

import httpx

from backend.feedback import USER_VERDICTS
from backend.fingerprint import SESSION_FP_HEADER
from model.normalize import MAX_INPUT_CHARS

ALLOWED_VERDICTS = USER_VERDICTS
MAX_DETAIL_CHARS = 500


class BackendError(RuntimeError):
    """The backend answered with a status the UI must surface rather than swallow."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"the backend returned HTTP {status_code}")
        self.status_code = status_code
        self.detail = detail[:MAX_DETAIL_CHARS]


class RateLimited(BackendError):
    """429. The user is told to slow down; nothing is retried automatically."""


def new_session_fp() -> str:
    return secrets.token_hex(8)


@dataclass
class BackendClient:
    base_url: str
    api_key: str = field(repr=False)
    session_fp: str = field(repr=False)
    timeout: float = 10.0
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url, timeout=self.timeout, transport=self.transport
        )

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"X-API-Key": self.api_key, SESSION_FP_HEADER: self.session_fp}
        headers.update(extra or {})
        return headers

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = response.text
        error = RateLimited if response.status_code == 429 else BackendError
        raise error(response.status_code, detail)

    def predict(self, text: str) -> dict:
        if len(text) > MAX_INPUT_CHARS:
            raise ValueError(f"comment exceeds MAX_INPUT_CHARS={MAX_INPUT_CHARS}")
        with self._client() as client:
            response = client.post("/predict", json={"text": text}, headers=self._headers())
        self._raise_for_status(response)
        return response.json()

    def user_feedback(self, request_id: str, verdict: str) -> dict:
        if verdict not in ALLOWED_VERDICTS:
            raise ValueError(f"verdict must be one of {sorted(ALLOWED_VERDICTS)}")
        with self._client() as client:
            response = client.post(
                "/feedback/user",
                json={"request_id": request_id, "verdict": verdict},
                headers=self._headers(),
            )
        self._raise_for_status(response)
        return response.json()

    def login(self, secret: str) -> str:
        with self._client() as client:
            response = client.post("/review/login", json={"secret": secret})
        self._raise_for_status(response)
        return response.json()["token"]

    def pending(self, token: str, limit: int = 20) -> list[dict]:
        with self._client() as client:
            response = client.get(
                "/review/pending",
                params={"limit": limit},
                headers=self._headers({"Authorization": f"Bearer {token}"}),
            )
        self._raise_for_status(response)
        return response.json()["items"]

    def submit(self, token: str, request_id: str, labels: dict[str, int]) -> dict:
        with self._client() as client:
            response = client.post(
                "/review/submit",
                json={"request_id": request_id, "labels": labels},
                headers=self._headers({"Authorization": f"Bearer {token}"}),
            )
        self._raise_for_status(response)
        return response.json()
