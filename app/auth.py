"""Okta / OIDC single sign-on.

When the OKTA_* config vars are set, the app authenticates users through Okta
(OpenID Connect Authorization Code flow) instead of the shared APP_PASSWORD.
Access is gated by Okta app assignment — only users assigned to the Okta app
can complete a login, so the app trusts any successful login. Everything is
env-var driven so no secrets live in the repo.

Authlib is imported lazily so offline dev (no SSO configured) doesn't need it.
"""
import logging
import os

log = logging.getLogger(__name__)

try:
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth()
    _AUTHLIB = True
except ImportError:                       # pragma: no cover - only when SSO unused
    oauth = None
    _AUTHLIB = False


def okta_enabled() -> bool:
    """True when Okta SSO is configured (issuer + client credentials present)."""
    return _AUTHLIB and all(os.environ.get(k) for k in
                            ('OKTA_ISSUER', 'OKTA_CLIENT_ID', 'OKTA_CLIENT_SECRET'))


def init_okta(app) -> None:
    """Register the Okta OIDC client from its discovery document. No-op unless
    Okta is configured; warns if it's half-configured (or Authlib is missing)."""
    partly = any(os.environ.get(k) for k in
                 ('OKTA_ISSUER', 'OKTA_CLIENT_ID', 'OKTA_CLIENT_SECRET'))
    if not okta_enabled():
        if partly and not _AUTHLIB:
            log.warning("OKTA_* vars set but Authlib isn't installed — SSO disabled.")
        elif partly:
            log.warning("Okta SSO is only partly configured — need OKTA_ISSUER, "
                        "OKTA_CLIENT_ID and OKTA_CLIENT_SECRET. SSO disabled.")
        return

    issuer = os.environ['OKTA_ISSUER'].rstrip('/')
    oauth.init_app(app)
    oauth.register(
        name='okta',
        client_id=os.environ['OKTA_CLIENT_ID'],
        client_secret=os.environ['OKTA_CLIENT_SECRET'],
        server_metadata_url=f'{issuer}/.well-known/openid-configuration',
        client_kwargs={
            'scope': os.environ.get('OKTA_SCOPES', 'openid email profile'),
            # U-M's Okta app requires PKCE; Authlib then generates the code
            # verifier/challenge and completes the exchange with it. Harmless if
            # PKCE isn't required, so always on.
            'code_challenge_method': 'S256',
        },
    )
    log.info("Okta SSO enabled (issuer %s)", issuer)
