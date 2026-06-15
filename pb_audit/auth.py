"""Authentification Shopify via le grant OAuth `client_credentials`.

Le jeton d'accès est obtenu en POSTant les identifiants client à
`/admin/oauth/access_token`, puis utilisé dans le header
`X-Shopify-Access-Token` de tous les appels Admin API.

Sécurité :
  * Le token est gardé EN MÉMOIRE uniquement, jamais écrit sur disque.
  * Si la réponse contient `expires_in`, l'expiration est gérée et le token
    est redemandé automatiquement quand il est (presque) périmé.
  * Ni le token ni le secret ne sont logués (le corps de réponse n'est jamais
    logué en clair).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .config import Secrets
from .logging_setup import get_logger

# Marge de sécurité : on renouvelle le token un peu avant son expiration réelle.
_EXPIRY_SAFETY_MARGIN_S = 60


class AuthError(Exception):
    """Échec de l'obtention d'un jeton d'accès."""


@dataclass
class _Token:
    value: str
    # Timestamp monotone d'expiration ; None = pas d'expiration connue.
    expires_at: float | None


class TokenProvider:
    """Fournit un jeton d'accès valide, en le renouvelant à la demande.

    Thread-affine : conçu pour un usage mono-thread (l'outil est séquentiel).
    """

    def __init__(self, secrets: Secrets, session: requests.Session | None = None):
        self._secrets = secrets
        self._session = session or requests.Session()
        self._token: _Token | None = None
        self._log = get_logger()

    @property
    def _token_url(self) -> str:
        return f"{self._secrets.base_url}/admin/oauth/access_token"

    def _is_valid(self) -> bool:
        if self._token is None:
            return False
        if self._token.expires_at is None:
            return True  # pas d'expiration annoncée -> on garde
        return time.monotonic() < (self._token.expires_at - _EXPIRY_SAFETY_MARGIN_S)

    def _request_token(self) -> _Token:
        """Demande un nouveau jeton via client_credentials."""
        self._log.info("Demande d'un nouveau jeton (client_credentials)…")
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._secrets.client_id,
            "client_secret": self._secrets.client_secret,
        }
        try:
            resp = self._session.post(
                self._token_url,
                json=payload,
                headers={"Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise AuthError(f"Requête de jeton échouée : {exc}") from exc

        if resp.status_code != 200:
            # On ne logue jamais le corps brut (peut contenir des détails).
            raise AuthError(
                f"Obtention du jeton refusée (HTTP {resp.status_code}). "
                "Vérifiez SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET et que "
                "l'app utilise bien le grant client_credentials."
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise AuthError("Réponse de jeton non-JSON.") from exc

        token_value = data.get("access_token")
        if not token_value:
            raise AuthError("Réponse de jeton sans champ access_token.")

        expires_at: float | None = None
        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expires_at = time.monotonic() + float(expires_in)
            self._log.info(
                "Jeton obtenu (expire dans %ss).", int(expires_in)
            )
        else:
            self._log.info("Jeton obtenu (pas d'expiration annoncée).")

        return _Token(value=token_value, expires_at=expires_at)

    def get_token(self, force_refresh: bool = False) -> str:
        """Retourne un jeton valide, en le renouvelant si nécessaire."""
        if force_refresh or not self._is_valid():
            self._token = self._request_token()
        assert self._token is not None
        return self._token.value

    def invalidate(self) -> None:
        """Force le renouvellement au prochain appel (ex: après un 401)."""
        self._log.debug("Invalidation du jeton en cache.")
        self._token = None
