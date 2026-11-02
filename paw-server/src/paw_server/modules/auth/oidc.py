"""Google OpenID Connect (authorization code + PKCE). Business code depends only on `IdentityProvider`.

The ID token is verified here: signature (Google's published keys), issuer, audience, expiry, nonce and
`email_verified`. Nothing from the token is trusted before that.
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import jwt

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ["https://accounts.google.com", "accounts.google.com"]


class IdentityError(Exception):
    """Sign-in could not be verified. `code` is a short machine-safe reason; never contains token content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Identity:
    email: str  # lower-cased
    subject: str


class IdentityProvider(Protocol):
    def authorize_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str, prompt: str
    ) -> str: ...

    def identity_from_code(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> Identity: ...


def new_pkce() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


class GoogleIdentityProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.Client | None = None,
        jwks_client: Any = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.client_id, self._secret = client_id, client_secret
        self._http = http or httpx.Client(timeout=timeout_s)
        # PyJWKClient caches Google's keys; tests inject a stub with get_signing_key_from_jwt()
        self._jwks = jwks_client or jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)

    def authorize_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str, prompt: str
    ) -> str:
        q = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": prompt,  # select_account for login; login (forces re-auth) for step-up
        }
        return f"{AUTH_URL}?{urlencode(q)}"

    def identity_from_code(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> Identity:
        try:
            r = self._http.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self._secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
            )
        except httpx.HTTPError as exc:
            raise IdentityError("token_endpoint_unreachable") from exc
        if r.status_code != 200:
            raise IdentityError("code_exchange_failed")
        try:
            id_token = r.json()["id_token"]
        except (KeyError, ValueError) as exc:
            raise IdentityError("no_id_token") from exc
        return self.verify_id_token(id_token, nonce)

    def verify_id_token(self, id_token: str, nonce: str) -> Identity:
        try:
            key = self._jwks.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=ISSUERS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise IdentityError("invalid_id_token") from exc
        except Exception as exc:  # keys unreachable etc.: fail closed
            raise IdentityError("keys_unavailable") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise IdentityError("nonce_mismatch")
        email = str(claims.get("email", "")).strip().lower()
        if not email or claims.get("email_verified") is not True:
            raise IdentityError("email_not_verified")
        return Identity(email=email, subject=str(claims["sub"]))
