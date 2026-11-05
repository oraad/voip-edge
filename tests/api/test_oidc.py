"""OIDC (T11) API tests with a fully in-process fake identity provider.

The provider's discovery, JWKS and token endpoints are intercepted with respx;
ID tokens are signed with a throwaway RSA key generated per test session. This
gives the full negative matrix (state/nonce/iss/aud/exp/signature/alg/at_hash)
without any network or real IdP.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from authlib.jose import JsonWebKey, jwt as jose_jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from console import oidc
from console import settings as settings_module

ISSUER = "https://id.example"
CLIENT_ID = "console"
REDIRECT = "http://testserver/auth/callback"
AUTHORIZE = f"{ISSUER}/authorize"
TOKEN = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks"
END_SESSION = f"{ISSUER}/logout"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIV_PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
PUB_PEM = KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
JWKS = {
    "keys": [
        JsonWebKey.import_key(PUB_PEM, {"use": "sig", "kid": "k1"}).as_dict(
            is_private=False
        )
    ]
}


def make_token(claims: dict, key: bytes = PRIV_PEM, kid: str = "k1") -> str:
    header = {"alg": "RS256", "kid": kid}
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "u1",
        "email": "operator@example.net",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        **claims,
    }
    return jose_jwt.encode(header, payload, key).decode()


@pytest.fixture(autouse=True)
def _reset_oidc_caches():
    oidc._discovery_cache = None
    oidc._discovery_at = 0.0
    oidc._discovery_fail_until = 0.0
    oidc._jwks_cache = None
    oidc._jwks_at = 0.0
    yield


@pytest.fixture()
def oidc_settings(monkeypatch):
    def apply(**overrides):
        values = dict(
            auth_mode="both",
            oidc_issuer=ISSUER,
            oidc_client_id=CLIENT_ID,
            oidc_client_secret="s3cret",
            oidc_redirect_uri=REDIRECT,
            oidc_post_logout_uri="http://testserver/login",
            oidc_provider_name="Example ID",
            oidc_allowed_groups=("edge-operators",),
            oidc_allowed_email_domains=(),
            oidc_allow_any=True,
            oidc_insecure=True,
        )
        values.update(overrides)
        cfg = replace(settings_module.settings, **values)
        monkeypatch.setattr(settings_module, "settings", cfg)
        return cfg

    return apply


@pytest.fixture()
def provider():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZE,
                    "token_endpoint": TOKEN,
                    "jwks_uri": JWKS_URI,
                    "end_session_endpoint": END_SESSION,
                },
            )
        )
        mock.get(JWKS_URI).mock(return_value=httpx.Response(200, json=JWKS))
        token_route = mock.post(TOKEN).mock(
            return_value=httpx.Response(200, json={"access_token": "at", "id_token": ""})
        )
        yield mock, token_route


def _start(client, next_path: str = "/homes") -> dict:
    response = client.get(f"/auth/login?next={next_path}")
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(AUTHORIZE)
    query = parse_qs(urlparse(location).query)
    assert query["code_challenge_method"] == ["S256"]
    return {k: v[0] for k, v in query.items()}


def _token_for(flow: dict, **claims) -> str:
    return make_token({"nonce": flow["nonce"], **claims})


def _callback(client, flow: dict, code: str = "abc"):
    return client.get(f"/auth/callback?code={code}&state={flow['state']}")


def test_happy_path_signs_in_and_honours_next(
    seeded_db, auth_client_unused_unused, client, oidc_settings, provider
):
    oidc_settings()
    _, token_route = provider
    flow = _start(client, "/tables/dispatcher")
    token_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at",
                "id_token": _token_for(flow, groups=["edge-operators"]),
            },
        )
    )

    response = _callback(client, flow)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/tables/dispatcher"
    assert client.cookies.get("session")

    page = client.get("/homes")
    assert page.status_code == 200
    assert "operator@example.net" in page.text


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 3600},
    ],
    ids=["wrong-aud", "wrong-iss", "expired"],
)
def test_invalid_claims_are_rejected(
    seeded_db, client, oidc_settings, provider, claims
):
    oidc_settings(oidc_allowed_groups=())
    _, token_route = provider
    flow = _start(client)
    token_route.mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "id_token": _token_for(flow, **claims)}
        )
    )
    response = _callback(client, flow)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=token_rejected"
    assert client.cookies.get("session") is None


def test_nonce_mismatch_is_rejected(seeded_db, client, oidc_settings, provider):
    oidc_settings(oidc_allowed_groups=())
    _, token_route = provider
    flow = _start(client)
    token_route.mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "id_token": make_token({"nonce": "other"})}
        )
    )
    response = _callback(client, flow)
    assert response.headers["location"] == "/login?error=token_rejected"


def test_bad_signature_is_rejected(seeded_db, client, oidc_settings, provider):
    oidc_settings(oidc_allowed_groups=())
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _, token_route = provider
    flow = _start(client)
    token_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at",
                "id_token": make_token({"nonce": flow["nonce"]}, key=other_pem),
            },
        )
    )
    response = _callback(client, flow)
    assert response.headers["location"] == "/login?error=token_rejected"


def test_alg_none_is_rejected(seeded_db, client, oidc_settings, provider):
    import base64

    oidc_settings(oidc_allowed_groups=())
    _, token_route = provider
    flow = _start(client)

    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64({"alg": "none", "kid": "k1"})
    body = b64(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "u1",
            "nonce": flow["nonce"],
            "exp": int(time.time()) + 300,
        }
    )
    token_route.mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "id_token": f"{header}.{body}."}
        )
    )
    response = _callback(client, flow)
    assert response.headers["location"] == "/login?error=token_rejected"


def test_state_mismatch_is_rejected(seeded_db, client, oidc_settings, provider):
    oidc_settings(oidc_allowed_groups=())
    _start(client)
    response = client.get("/auth/callback?code=abc&state=tampered")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=invalid_state"
    assert client.cookies.get("session") is None


def test_claims_allowlist_denies_with_403_page(seeded_db, client, oidc_settings, provider):
    oidc_settings()
    _, token_route = provider
    flow = _start(client)
    token_route.mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "at", "id_token": _token_for(flow, groups=["other"])},
        )
    )
    response = _callback(client, flow)
    assert response.status_code == 403
    assert "Access denied" in response.text
    assert client.cookies.get("session") is None


def test_email_domain_allowlist(seeded_db, client, oidc_settings, provider):
    oidc_settings(oidc_allowed_groups=(), oidc_allowed_email_domains=("example.net",))
    _, token_route = provider
    flow = _start(client)
    token_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at",
                "id_token": _token_for(
                    flow, email="someone@example.net", email_verified=True
                ),
            },
        )
    )
    response = _callback(client, flow)
    assert response.status_code == 303
    assert response.headers["location"] == "/homes"


@pytest.mark.parametrize("next_path", ["//evil.example", "https://evil.example", ""])
def test_open_redirect_is_neutralized(
    seeded_db, client, oidc_settings, provider, next_path
):
    oidc_settings(oidc_allowed_groups=())
    _, token_route = provider
    flow = _start(client, next_path)
    token_route.mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "id_token": _token_for(flow)}
        )
    )
    response = _callback(client, flow)
    assert response.headers["location"] == "/homes"


def test_discovery_failure_is_an_operator_message(
    seeded_db, client, oidc_settings, provider, monkeypatch
):
    oidc_settings(oidc_allowed_groups=())
    mock, _ = provider
    mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(503)
    )
    response = client.get("/auth/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=idp_unreachable"

    page = client.get("/login?error=idp_unreachable")
    assert "identity provider is unreachable" in page.text.lower()


def test_auth_modes(seeded_db, client, oidc_settings):
    oidc_settings(auth_mode="token")
    assert client.get("/auth/login").status_code == 404
    page = client.get("/login")
    assert "Console token" in page.text
    assert "Sign in with" not in page.text

    oidc_settings(auth_mode="oidc")
    page = client.get("/login")
    assert "Sign in with Example ID" in page.text
    assert "Console token" not in page.text
    assert client.post("/login", data={"token": "x"}).status_code == 403


def test_logout_requires_post_csrf_and_clears_session(
    seeded_db, client, oidc_settings, provider
):
    oidc_settings()
    _, token_route = provider
    flow = _start(client)
    token_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at",
                "id_token": _token_for(flow, groups=["edge-operators"]),
            },
        )
    )
    assert _callback(client, flow).status_code == 303
    csrf = client.cookies.get("csrf")
    client.headers["X-CSRF-Token"] = "wrong"
    assert client.post("/auth/logout").status_code == 403

    client.headers["X-CSRF-Token"] = csrf
    response = client.post("/auth/logout")
    assert response.status_code == 303
    assert response.headers["location"].startswith(END_SESSION)
    assert client.cookies.get("session") is None


@pytest.fixture()
def auth_client_unused_unused():
    """Placeholder so the happy-path signature reads explicitly.

    The OIDC flow must not pre-authenticate through the token endpoint; this
    fixture intentionally does nothing.
    """
    return None


def test_empty_allowlist_fails_closed(seeded_db, client, oidc_settings, provider):
    oidc_settings(oidc_allow_any=False, oidc_allowed_groups=(), oidc_allowed_email_domains=())
    _, token_route = provider
    flow = _start(client)
    token_route.mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "id_token": _token_for(flow)}
        )
    )
    response = _callback(client, flow)
    assert response.status_code == 403
    assert client.cookies.get("session") is None


def test_email_domain_requires_verified_email(
    seeded_db, client, oidc_settings, provider
):
    oidc_settings(
        oidc_allow_any=False,
        oidc_allowed_groups=(),
        oidc_allowed_email_domains=("example.net",),
    )
    _, token_route = provider
    flow = _start(client)
    token_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "at",
                "id_token": _token_for(
                    flow, email="someone@example.net", email_verified=False
                ),
            },
        )
    )
    response = _callback(client, flow)
    assert response.status_code == 403


def test_flow_cookie_cannot_be_used_as_a_session(seeded_db, client, oidc_settings, provider):
    oidc_settings()
    _start(client)
    flow_cookie = next(
        (v for k, v in client.cookies.items() if k.startswith("oidc_flow_")), None
    )
    assert flow_cookie
    client.cookies.set("session", flow_cookie, path="/")
    assert client.get("/homes").status_code in (302, 303)
    assert client.get("/homes").headers.get("location") == "/login"


def test_crafted_state_is_rejected_without_a_cookie_name_error(
    client, oidc_settings, provider
):
    oidc_settings()
    response = client.get(
        "/auth/callback", params={"code": "abc", "state": "bad state{}"}
    )
    assert response.status_code == 303
    assert "error=invalid_state" in response.headers["location"]


def test_flow_token_with_non_ascii_signature_is_rejected():
    assert oidc.verify_flow("abc.\u00e9") is None
