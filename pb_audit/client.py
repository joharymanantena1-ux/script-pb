"""Client Admin GraphQL robuste.

Gère :
  * l'injection du header X-Shopify-Access-Token (jamais logué) ;
  * le rate limit basé sur le COÛT (extensions.cost.throttleStatus) avec
    back-off exponentiel quand le budget restant est bas ;
  * les retries sur erreurs transitoires : 401 -> re-auth, 429 -> respect du
    Retry-After / restore rate, 5xx -> back-off exponentiel ;
  * la remontée explicite des `userErrors` GraphQL au niveau appelant.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from .auth import TokenProvider
from .logging_setup import get_logger

# Si le budget de coût restant tombe sous ce seuil, on attend une restauration.
_LOW_BUDGET_THRESHOLD = 100
# Plafond du back-off exponentiel (secondes).
_MAX_BACKOFF_S = 30.0
# Nombre maximum de tentatives par requête sur erreurs transitoires.
_MAX_RETRIES = 6


class GraphQLError(Exception):
    """Erreur GraphQL non transitoire (la requête ne réussira pas en l'état)."""


class GraphQLHTTPError(Exception):
    """Erreur HTTP non récupérable après épuisement des retries."""


@dataclass
class CostStatus:
    requested: float
    actual: float
    available: float
    maximum: float
    restore_rate: float

    @classmethod
    def from_extensions(cls, extensions: dict[str, Any]) -> "CostStatus | None":
        cost = (extensions or {}).get("cost")
        if not cost:
            return None
        throttle = cost.get("throttleStatus", {})
        return cls(
            requested=float(cost.get("requestedQueryCost", 0) or 0),
            actual=float(cost.get("actualQueryCost", 0) or 0),
            available=float(throttle.get("currentlyAvailable", 0) or 0),
            maximum=float(throttle.get("maximumAvailable", 0) or 0),
            restore_rate=float(throttle.get("restoreRate", 0) or 0),
        )


class ShopifyGraphQLClient:
    """Client GraphQL séquentiel avec gestion du coût et des retries."""

    def __init__(
        self,
        token_provider: TokenProvider,
        api_version: str,
        session: requests.Session | None = None,
    ):
        self._tokens = token_provider
        self._api_version = api_version
        self._session = session or requests.Session()
        self._log = get_logger()
        self._last_cost: CostStatus | None = None

    @property
    def endpoint(self) -> str:
        shop_url = self._tokens._secrets.base_url  # noqa: SLF001 (accès interne assumé)
        return f"{shop_url}/admin/api/{self._api_version}/graphql.json"

    @property
    def last_cost(self) -> CostStatus | None:
        return self._last_cost

    # --------------------------------------------------------------------- #
    # Gestion proactive du budget de coût
    # --------------------------------------------------------------------- #
    def _maybe_wait_for_budget(self, next_cost_estimate: float) -> None:
        """Attend que le budget se restaure si on risque de dépasser."""
        cost = self._last_cost
        if cost is None:
            return
        # Si le budget disponible ne couvre pas l'estimation + une marge, on patiente.
        needed = max(next_cost_estimate, _LOW_BUDGET_THRESHOLD)
        if cost.available >= needed:
            return
        if cost.restore_rate <= 0:
            return
        deficit = needed - cost.available
        wait_s = min(deficit / cost.restore_rate, _MAX_BACKOFF_S)
        if wait_s > 0:
            self._log.info(
                "Budget de coût bas (%.0f/%.0f dispo). Pause %.1fs pour restauration.",
                cost.available,
                cost.maximum,
                wait_s,
            )
            time.sleep(wait_s)

    # --------------------------------------------------------------------- #
    # Exécution d'une requête / mutation
    # --------------------------------------------------------------------- #
    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        estimated_cost: float = 50.0,
    ) -> dict[str, Any]:
        """Exécute une opération GraphQL et retourne le bloc `data`.

        Lève GraphQLError sur erreurs GraphQL non transitoires, ou
        GraphQLHTTPError si les retries sont épuisés.
        """
        self._maybe_wait_for_budget(estimated_cost)

        attempt = 0
        backoff = 1.0
        while True:
            attempt += 1
            token = self._tokens.get_token()
            try:
                resp = self._session.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-Shopify-Access-Token": token,
                    },
                    timeout=120,
                )
            except requests.RequestException as exc:
                if attempt >= _MAX_RETRIES:
                    raise GraphQLHTTPError(
                        f"Erreur réseau après {attempt} tentatives : {exc}"
                    ) from exc
                self._log.warning(
                    "Erreur réseau (tentative %d/%d) : %s. Back-off %.1fs.",
                    attempt, _MAX_RETRIES, exc, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue

            # --- 401 : jeton périmé / invalide -> re-auth puis retry --------
            if resp.status_code == 401:
                if attempt >= _MAX_RETRIES:
                    raise GraphQLHTTPError("401 persistant après re-auth.")
                self._log.warning("HTTP 401 : renouvellement du jeton et retry.")
                self._tokens.invalidate()
                continue

            # --- 429 : throttling HTTP -> respect Retry-After --------------
            if resp.status_code == 429:
                if attempt >= _MAX_RETRIES:
                    raise GraphQLHTTPError("429 persistant (rate limit).")
                retry_after = float(resp.headers.get("Retry-After", backoff))
                wait_s = min(max(retry_after, 1.0), _MAX_BACKOFF_S)
                self._log.warning(
                    "HTTP 429 (rate limit). Pause %.1fs puis retry (%d/%d).",
                    wait_s, attempt, _MAX_RETRIES,
                )
                time.sleep(wait_s)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue

            # --- 5xx : transitoire -> back-off exponentiel -----------------
            if 500 <= resp.status_code < 600:
                if attempt >= _MAX_RETRIES:
                    raise GraphQLHTTPError(
                        f"HTTP {resp.status_code} persistant après {attempt} tentatives."
                    )
                self._log.warning(
                    "HTTP %d (transitoire). Back-off %.1fs puis retry (%d/%d).",
                    resp.status_code, backoff, attempt, _MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue

            if resp.status_code != 200:
                raise GraphQLHTTPError(
                    f"HTTP {resp.status_code} inattendu (non récupérable)."
                )

            # --- 200 : analyse du corps GraphQL ----------------------------
            try:
                body = resp.json()
            except ValueError as exc:
                raise GraphQLHTTPError("Réponse 200 non-JSON.") from exc

            # Mise à jour du suivi de coût.
            self._last_cost = CostStatus.from_extensions(body.get("extensions", {}))

            errors = body.get("errors")
            if errors:
                # THROTTLED est transitoire : on attend la restauration et on retry.
                if self._is_throttled(errors):
                    if attempt >= _MAX_RETRIES:
                        raise GraphQLHTTPError("THROTTLED persistant (coût GraphQL).")
                    wait_s = self._throttle_wait()
                    self._log.warning(
                        "GraphQL THROTTLED. Pause %.1fs puis retry (%d/%d).",
                        wait_s, attempt, _MAX_RETRIES,
                    )
                    time.sleep(wait_s)
                    continue
                # Autres erreurs GraphQL : non transitoires.
                raise GraphQLError(f"Erreurs GraphQL : {errors}")

            data = body.get("data")
            if data is None:
                raise GraphQLError("Réponse GraphQL sans bloc `data`.")
            return data

    @staticmethod
    def _is_throttled(errors: list[dict[str, Any]]) -> bool:
        for err in errors:
            code = (err.get("extensions") or {}).get("code")
            if code == "THROTTLED":
                return True
        return False

    def _throttle_wait(self) -> float:
        """Calcule le temps d'attente pour restaurer assez de budget."""
        cost = self._last_cost
        if cost and cost.restore_rate > 0:
            deficit = max(_LOW_BUDGET_THRESHOLD - cost.available, 0)
            return min(max(deficit / cost.restore_rate, 1.0), _MAX_BACKOFF_S)
        return 2.0
