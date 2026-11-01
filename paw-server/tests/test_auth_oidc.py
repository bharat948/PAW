"""Google ID-token verification and sign-in helpers. Real RSA keys, no network."""

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from paw_server.modules.auth.oidc import GoogleIdentityProvider, IdentityError, new_pkce
from paw_server.modules.auth.service import safe_return_to

CLIENT_ID = "client-123.apps.googleusercontent.com"
NONCE = "nonce-abc"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def claims(**over):
    now = int(time.time())
    base = dict(
        iss="https://accounts.google.com",
        aud=CLIENT_ID,
        sub="1234567890",
        email="Owner@Example.org",
        email_verified=True,
        nonce=NONCE,
        iat=now,
        exp=now + 300,
    )
    return {**base, **over}


def provider(id_token: str | None, status: int = 200) -> GoogleIdentityProvider:
    def handler(req: httpx.Request) -> httpx.Response:
        body = {"id_token": id_token} if id_token is not None else {}
        return httpx.Response(status, json=body)

    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key()))
    return GoogleIdentityProvider(
        CLIENT_ID,
        "secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        jwks_client=jwks,
    )


def sign(payload: dict, key=KEY, alg="RS256") -> str:
    return jwt.encode(payload, key, algorithm=alg)


def exchange(p: GoogleIdentityProvider, nonce: str = NONCE):
    return p.identity_from_code(
        code="c", redirect_uri="http://x/cb", code_verifier="v", nonce=nonce
    )


def test_valid_token_yields_a_lowercased_verified_identity():
    ident = exchange(provider(sign(claims())))
    assert ident.email == "owner@example.org" and ident.subject == "1234567890"


def test_both_google_issuer_spellings_are_accepted():
    assert exchange(provider(sign(claims(iss="accounts.google.com")))).email


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"aud": "someone-elses-client"}, "invalid_id_token"),
        ({"iss": "https://evil.example"}, "invalid_id_token"),
        ({"exp": int(time.time()) - 10}, "invalid_id_token"),
        ({"nonce": "other"}, "nonce_mismatch"),
        ({"email_verified": False}, "email_not_verified"),
        ({"email_verified": "true"}, "email_not_verified"),  # must be the boolean true
        ({"email": ""}, "email_not_verified"),
    ],
)
def test_bad_claims_are_rejected(over, reason):
    with pytest.raises(IdentityError) as e:
        exchange(provider(sign(claims(**over))))
    assert e.value.code == reason


def test_token_signed_by_another_key_is_rejected():
    with pytest.raises(IdentityError) as e:
        exchange(provider(sign(claims(), key=OTHER_KEY)))
    assert e.value.code == "invalid_id_token"


def test_algorithm_confusion_hs256_with_the_public_key_is_rejected():
    """Classic attack: sign with HMAC using the (public) RSA key as the secret. Built by hand: PyJWT refuses to."""
    pub = KEY.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def b64(b: bytes) -> bytes:
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    signing_input = (
        b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        + b"."
        + b64(json.dumps(claims()).encode())
    )
    forged = (
        signing_input + b"." + b64(hmac.new(pub, signing_input, hashlib.sha256).digest())
    ).decode()
    with pytest.raises(IdentityError):
        exchange(provider(forged))


def test_alg_none_is_rejected():
    forged = jwt.encode(claims(), key=None, algorithm="none")
    with pytest.raises(IdentityError):
        exchange(provider(forged))


def test_missing_required_claim_is_rejected():
    payload = claims()
    del payload["exp"]
    with pytest.raises(IdentityError):
        exchange(provider(sign(payload)))


def test_token_endpoint_failure_and_missing_id_token():
    with pytest.raises(IdentityError) as e:
        exchange(provider("x", status=400))
    assert e.value.code == "code_exchange_failed"
    with pytest.raises(IdentityError) as e:
        exchange(provider(None))
    assert e.value.code == "no_id_token"


def test_token_endpoint_unreachable_fails_closed():
    def boom(req):
        raise httpx.ConnectError("down")

    p = GoogleIdentityProvider(
        CLIENT_ID, "s", http=httpx.Client(transport=httpx.MockTransport(boom))
    )
    with pytest.raises(IdentityError) as e:
        exchange(p)
    assert e.value.code == "token_endpoint_unreachable"


def test_keys_unavailable_fails_closed():
    def broken(_):
        raise RuntimeError("no keys")

    p = provider(sign(claims()))
    p._jwks = SimpleNamespace(get_signing_key_from_jwt=broken)
    with pytest.raises(IdentityError) as e:
        exchange(p)
    assert e.value.code == "keys_unavailable"


def test_authorize_url_has_pkce_state_nonce_and_no_secret():
    url = provider("x").authorize_url(
        redirect_uri="http://localhost:8000/v1/auth/google/callback",
        state="S",
        nonce="N",
        code_challenge="C",
        prompt="select_account",
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    for part in (
        "state=S",
        "nonce=N",
        "code_challenge=C",
        "code_challenge_method=S256",
        "response_type=code",
    ):
        assert part in url
    assert "secret" not in url


def test_pkce_challenge_matches_verifier():
    verifier, challenge = new_pkce()
    expect = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert challenge == expect and len(verifier) >= 43


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "/"),
        ("", "/"),
        ("/", "/"),
        ("/#/ask", "/#/ask"),
        ("/documents?x=1", "/documents?x=1"),
        ("//evil.example", "/"),
        ("https://evil.example", "/"),
        ("/\\evil.example", "/"),
        ("/ok\nSet-Cookie: a=b", "/"),
        ("javascript:alert(1)", "/"),
        ("/x" + "a" * 300, "/"),
        ("/redirect?u=https://evil.example", "/"),
    ],
)
def test_return_to_cannot_be_an_open_redirect(value, expected):
    assert safe_return_to(value) == expected
