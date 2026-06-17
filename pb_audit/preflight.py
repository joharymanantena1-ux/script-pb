"""Module 1 — Test de connexion (préflight).

Obtient un jeton, interroge la boutique, affiche ses infos, vérifie les scopes
requis (read_products, write_products) et que la version d'API demandée est
supportée. Échoue tôt avec un message clair en cas de problème.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import GraphQLError, GraphQLHTTPError, ShopifyGraphQLClient
from .logging_setup import get_logger

REQUIRED_SCOPES = ("read_products", "write_products")

# Infos boutique + version d'API + scopes de l'app installée.
_SHOP_QUERY = """
query Preflight {
  shop {
    name
    myshopifyDomain
    plan { displayName }
    currencyCode
  }
  currentAppInstallation {
    accessScopes { handle }
  }
}
"""

# Versions d'API supportées (pour valider api_version tôt).
_API_VERSIONS_QUERY = """
query ApiVersions {
  publicApiVersions { handle supported }
}
"""


@dataclass
class PreflightResult:
    shop_name: str
    domain: str
    plan: str
    currency: str
    api_version: str
    granted_scopes: list[str]
    missing_scopes: list[str]
    ok: bool


class PreflightError(Exception):
    """Échec du préflight (auth, scopes ou version)."""


def _check_api_version(client: ShopifyGraphQLClient, requested: str) -> None:
    """Vérifie que la version demandée est supportée par la boutique."""
    log = get_logger()
    try:
        data = client.execute(_API_VERSIONS_QUERY, estimated_cost=10)
    except (GraphQLError, GraphQLHTTPError) as exc:
        # Si l'endpoint répond mal, c'est souvent que la version d'URL est invalide.
        raise PreflightError(
            f"Impossible de valider la version d'API « {requested} ». "
            f"Vérifiez api.version dans la config. Détail : {exc}"
        ) from exc

    versions = data.get("publicApiVersions") or []
    supported = {v["handle"]: v.get("supported", False) for v in versions}
    if requested not in supported:
        raise PreflightError(
            f"Version d'API « {requested} » inconnue. "
            f"Versions disponibles : {sorted(supported)}"
        )
    if not supported[requested]:
        log.warning(
            "Version d'API « %s » connue mais marquée non supportée — "
            "envisagez une version stable plus récente.", requested,
        )


def run_preflight(client: ShopifyGraphQLClient, api_version: str) -> PreflightResult:
    """Exécute le préflight complet. Lève PreflightError si bloquant."""
    log = get_logger()

    # 1) Valide la version d'API (échoue tôt).
    _check_api_version(client, api_version)

    # 2) Infos boutique + scopes.
    try:
        data = client.execute(_SHOP_QUERY, estimated_cost=10)
    except (GraphQLError, GraphQLHTTPError) as exc:
        raise PreflightError(
            f"Échec de la requête d'info boutique : {exc}. "
            "Vérifiez l'authentification et les scopes de l'app."
        ) from exc

    shop = data.get("shop") or {}
    install = data.get("currentAppInstallation") or {}
    granted = sorted(
        s["handle"] for s in (install.get("accessScopes") or [])
    )
    missing = [s for s in REQUIRED_SCOPES if s not in granted]

    result = PreflightResult(
        shop_name=shop.get("name", "?"),
        domain=shop.get("myshopifyDomain", "?"),
        plan=(shop.get("plan") or {}).get("displayName", "?"),
        currency=shop.get("currencyCode", "?"),
        api_version=api_version,
        granted_scopes=granted,
        missing_scopes=missing,
        ok=not missing,
    )

    # Affichage récapitulatif.
    log.info("─" * 50)
    log.info("Boutique     : %s (%s)", result.shop_name, result.domain)
    log.info("Plan         : %s", result.plan)
    log.info("Devise       : %s", result.currency)
    log.info("Version API  : %s", result.api_version)
    log.info("Scopes       : %s", ", ".join(granted) or "(aucun listé)")
    log.info("─" * 50)

    if missing:
        raise PreflightError(
            "Scopes requis manquants : "
            + ", ".join(missing)
            + ". Ajoutez-les à la configuration de l'app puis réinstallez/"
            "réautorisez."
        )

    log.info("✓ Préflight OK : auth, scopes et version valides.")
    return result
