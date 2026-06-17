"""Récupération des produits + cache local.

Trois stratégies (config `cache.fetch_strategy`) :
  * paginate : pagination par curseurs (products connection).
  * bulk     : Bulk Operations (bulkOperationRunQuery -> JSONL).
  * auto     : paginate, mais bascule en bulk si le volume dépasse
               `cache.bulk_threshold`.

Le cache est un JSON horodaté sous `cache.dir`. Il NE contient JAMAIS le token
ni les secrets, uniquement les données produits normalisées. Relu s'il date de
moins de `cache.ttl_hours` ; `refresh=True` force un re-fetch.

Modèle de produit normalisé (dict) :
  {
    "id", "legacyResourceId", "title", "handle", "vendor", "status",
    "createdAt", "updatedAt", "hasImage", "bodyHtmlLen",
    "variants": [{"sku", "inventoryQuantity", "inventoryManagement"}],
    "totalInventory", "skus": [...], "collectionsCount"
  }
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from .client import ShopifyGraphQLClient
from .logging_setup import get_logger

# --------------------------------------------------------------------------- #
# Requêtes GraphQL
# --------------------------------------------------------------------------- #

# Compte rapide (préflight / décision auto vs bulk). productsCount renvoie
# une estimation/total selon la version ; on lit le champ `count`.
_COUNT_QUERY = """
query ProductsCount {
  productsCount { count }
}
"""

# Pagination par curseurs. On récupère jusqu'à 100 variantes par produit
# (suffisant pour la dédup par SKU ; les cas extrêmes sont rarissimes).
_PAGE_QUERY = """
query ProductsPage($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      legacyResourceId
      title
      handle
      vendor
      status
      createdAt
      updatedAt
      totalInventory
      featuredMedia { id }
      descriptionHtml
      collections(first: 1) { edges { node { id } } }
      variants(first: 100) {
        nodes {
          sku
          inventoryQuantity
          inventoryItem { tracked }
        }
      }
    }
  }
}
"""

# Requête Bulk : connexions imbriquées SANS arguments de pagination.
_BULK_QUERY = """
{
  products {
    edges {
      node {
        id
        legacyResourceId
        title
        handle
        vendor
        status
        createdAt
        updatedAt
        totalInventory
        featuredMedia { id }
        descriptionHtml
        collections { edges { node { id } } }
        variants {
          edges {
            node {
              id
              sku
              inventoryQuantity
              inventoryItem { tracked }
            }
          }
        }
      }
    }
  }
}
"""

_BULK_RUN_MUTATION = """
mutation BulkRun($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
"""

_BULK_POLL_QUERY = """
query BulkPoll {
  currentBulkOperation(type: QUERY) {
    id
    status
    errorCode
    objectCount
    url
  }
}
"""

# Annulation d'une opération bulk préexistante (une seule par boutique à la fois).
_BULK_CANCEL_MUTATION = """
mutation BulkCancel($id: ID!) {
  bulkOperationCancel(id: $id) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
"""


# --------------------------------------------------------------------------- #
# Normalisation d'un nœud produit -> dict plat
# --------------------------------------------------------------------------- #
def _normalize_product(node: dict[str, Any], variants: list[dict[str, Any]]) -> dict[str, Any]:
    """Convertit un nœud produit GraphQL en dict de domaine homogène."""
    skus: list[str] = []
    norm_variants: list[dict[str, Any]] = []
    for v in variants:
        sku = (v.get("sku") or "").strip()
        tracked = bool((v.get("inventoryItem") or {}).get("tracked", False))
        norm_variants.append(
            {
                "sku": sku,
                "inventoryQuantity": v.get("inventoryQuantity"),
                "tracked": tracked,
            }
        )
        if sku:
            skus.append(sku)

    body = node.get("descriptionHtml") or ""
    return {
        "id": node.get("id"),
        "legacyResourceId": node.get("legacyResourceId"),
        "title": node.get("title") or "",
        "handle": node.get("handle") or "",
        "vendor": node.get("vendor") or "",
        "status": node.get("status") or "",
        "createdAt": node.get("createdAt"),
        "updatedAt": node.get("updatedAt"),
        "totalInventory": node.get("totalInventory"),
        "hasImage": node.get("featuredMedia") is not None,
        "bodyHtmlLen": len(body.strip()),
        "variants": norm_variants,
        "skus": skus,
        "collectionsCount": _collections_count(node),
    }


def _collections_count(node: dict[str, Any]) -> int:
    cols = node.get("collections") or {}
    edges = cols.get("edges") or []
    # En pagination on ne demande que 1 collection (présence/absence) ;
    # en bulk on les a toutes. Dans les deux cas, >0 = "a au moins une collection".
    return len(edges)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
class ProductCache:
    """Cache JSON horodaté des produits (sans secret)."""

    def __init__(self, cache_dir: str | Path, shop: str):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Un fichier de cache par boutique.
        safe_shop = shop.replace(".", "_")
        self._path = self._dir / f"products-{safe_shop}.json"
        self._log = get_logger()

    @property
    def path(self) -> Path:
        return self._path

    def age_hours(self) -> float | None:
        if not self._path.is_file():
            return None
        mtime = self._path.stat().st_mtime
        return (time.time() - mtime) / 3600.0

    def is_fresh(self, ttl_hours: float) -> bool:
        age = self.age_hours()
        return age is not None and age <= ttl_hours

    def load(self) -> list[dict[str, Any]]:
        with self._path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload.get("products", [])

    def save(self, products: list[dict[str, Any]], source: str) -> None:
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": source,            # paginate | bulk
            "count": len(products),
            "products": products,        # AUCUN secret ici
        }
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        tmp.replace(self._path)
        self._log.info("Cache écrit (%d produits) : %s", len(products), self._path)


# --------------------------------------------------------------------------- #
# Fetcher
# --------------------------------------------------------------------------- #
class ProductFetcher:
    def __init__(self, client: ShopifyGraphQLClient, cfg, shop: str):
        self._client = client
        self._cfg = cfg
        self._cache = ProductCache(cfg.cache.dir, shop)
        self._log = get_logger()

    # -- API publique ---------------------------------------------------- #
    def get_products(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Retourne tous les produits (depuis le cache si frais, sinon fetch)."""
        ttl = self._cfg.cache.ttl_hours
        if not refresh and self._cache.is_fresh(ttl):
            age = self._cache.age_hours() or 0.0
            self._log.info(
                "Cache frais (%.1fh < TTL %.1fh) — relecture sans appel API.",
                age, ttl,
            )
            return self._cache.load()

        products = self._fetch_fresh()
        self._cache.save(products, source=self._chosen_source)
        return products

    def count(self) -> int:
        """Compte les produits (appel léger)."""
        data = self._client.execute(_COUNT_QUERY, estimated_cost=10)
        return int((data.get("productsCount") or {}).get("count", 0))

    # -- Stratégie ------------------------------------------------------- #
    def _fetch_fresh(self) -> list[dict[str, Any]]:
        strategy = self._cfg.cache.fetch_strategy
        if strategy == "paginate":
            self._chosen_source = "paginate"
            return self._fetch_paginate()
        if strategy == "bulk":
            self._chosen_source = "bulk"
            return self._fetch_bulk()

        # auto : on compte, puis on décide.
        total = self.count()
        self._log.info("Catalogue : ~%d produits.", total)
        if total >= self._cfg.cache.bulk_threshold:
            self._log.info(
                "Volume >= seuil bulk (%d) — Bulk Operations.",
                self._cfg.cache.bulk_threshold,
            )
            self._chosen_source = "bulk"
            return self._fetch_bulk()
        self._chosen_source = "paginate"
        return self._fetch_paginate()

    # -- Pagination par curseurs ---------------------------------------- #
    def _fetch_paginate(self) -> list[dict[str, Any]]:
        self._log.info("Récupération par pagination (curseurs)…")
        products: list[dict[str, Any]] = []
        cursor: str | None = None
        page = 0
        while True:
            page += 1
            data = self._client.execute(
                _PAGE_QUERY, variables={"cursor": cursor}, estimated_cost=120
            )
            conn = data["products"]
            for node in conn["nodes"]:
                variants = (node.get("variants") or {}).get("nodes", [])
                products.append(_normalize_product(node, variants))
            page_info = conn["pageInfo"]
            self._log.info("  page %d : %d produits cumulés.", page, len(products))
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]
        self._log.info("Pagination terminée : %d produits.", len(products))
        return products

    # -- Bulk Operations ------------------------------------------------- #
    def _fetch_bulk(self) -> list[dict[str, Any]]:
        # Une seule opération bulk QUERY par boutique à la fois : si une autre
        # est déjà en cours (CREATED/RUNNING), `currentBulkOperation` renverrait
        # SES résultats (URL d'un autre run) au lieu des nôtres. On l'annule
        # d'abord pour garantir qu'on lit bien notre propre opération.
        self._cancel_running_bulk()

        self._log.info("Lancement d'une Bulk Operation…")
        data = self._client.execute(
            _BULK_RUN_MUTATION, variables={"query": _BULK_QUERY}, estimated_cost=10
        )
        result = data["bulkOperationRunQuery"]
        errors = result.get("userErrors") or []
        if errors:
            raise RuntimeError(f"bulkOperationRunQuery a échoué : {errors}")
        op = result["bulkOperation"]
        self._log.info("Bulk Operation créée : %s (%s).", op["id"], op["status"])
        our_op_id = op["id"]

        url = self._poll_bulk(expected_id=our_op_id)
        if url is None:
            self._log.warning("Bulk Operation sans données (catalogue vide ?).")
            return []
        return self._download_and_parse_jsonl(url)

    def _cancel_running_bulk(self) -> None:
        """Annule toute opération bulk QUERY préexistante (CREATED/RUNNING)."""
        data = self._client.execute(_BULK_POLL_QUERY, estimated_cost=10)
        op = data.get("currentBulkOperation")
        if not op or op.get("status") not in ("CREATED", "RUNNING"):
            return
        self._log.warning(
            "Opération bulk préexistante détectée (%s, %s) — annulation.",
            op["id"], op["status"],
        )
        self._client.execute(
            _BULK_CANCEL_MUTATION, variables={"id": op["id"]}, estimated_cost=10
        )
        # Petite attente pour laisser l'annulation se propager.
        for _ in range(10):
            chk = self._client.execute(_BULK_POLL_QUERY, estimated_cost=10)
            cur = chk.get("currentBulkOperation") or {}
            if cur.get("status") not in ("CREATED", "RUNNING", "CANCELING"):
                return
            time.sleep(1.5)

    def _poll_bulk(self, expected_id: str | None = None) -> str | None:
        """Sonde l'opération courante jusqu'à COMPLETED ; back-off doux.

        Si `expected_id` est fourni, vérifie que l'opération sondée est bien la
        nôtre (sécurité contre une opération concurrente qui aurait pris le pas).
        """
        delay = 2.0
        while True:
            data = self._client.execute(_BULK_POLL_QUERY, estimated_cost=10)
            op = data.get("currentBulkOperation")
            if not op:
                raise RuntimeError("Aucune Bulk Operation courante à sonder.")
            if expected_id and op.get("id") != expected_id:
                raise RuntimeError(
                    "L'opération bulk courante n'est pas la nôtre "
                    f"(attendu {expected_id}, vu {op.get('id')}). "
                    "Une autre opération a démarré en parallèle — réessayez."
                )
            status = op["status"]
            self._log.info(
                "  bulk status=%s objets=%s", status, op.get("objectCount")
            )
            if status == "COMPLETED":
                return op.get("url")
            if status in ("FAILED", "CANCELED", "CANCELING"):
                raise RuntimeError(
                    f"Bulk Operation terminée en erreur : {status} "
                    f"(code={op.get('errorCode')})."
                )
            time.sleep(delay)
            delay = min(delay * 1.5, 15.0)

    def _download_and_parse_jsonl(self, url: str) -> list[dict[str, Any]]:
        """Télécharge le JSONL et reconstruit produits + variantes.

        Le JSONL bulk « aplatit » la hiérarchie : chaque ligne est soit un
        produit, soit une variante (avec `__parentId` pointant le produit).
        """
        self._log.info("Téléchargement du JSONL bulk…")
        resp = requests.get(url, timeout=300, stream=True)
        resp.raise_for_status()

        nodes_by_id: dict[str, dict[str, Any]] = {}
        children_by_parent: dict[str, list[dict[str, Any]]] = {}

        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            obj = json.loads(raw_line)
            obj_id = obj.get("id", "")
            parent = obj.get("__parentId")
            if parent:
                children_by_parent.setdefault(parent, []).append(obj)
            elif obj_id.startswith("gid://shopify/Product/"):
                nodes_by_id[obj_id] = obj
            # On ignore les autres types racine éventuels.

        products: list[dict[str, Any]] = []
        for pid, node in nodes_by_id.items():
            children = children_by_parent.get(pid, [])
            variants = [c for c in children if c.get("id", "").startswith(
                "gid://shopify/ProductVariant/")]
            # Les collections arrivent aussi comme enfants en bulk.
            collections = [c for c in children if c.get("id", "").startswith(
                "gid://shopify/Collection/")]
            normalized = _normalize_product(node, variants)
            # En bulk, le compte de collections vient des enfants aplatis.
            normalized["collectionsCount"] = len(collections)
            products.append(normalized)

        self._log.info("JSONL parsé : %d produits.", len(products))
        return products
