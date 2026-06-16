"""Module 4 — Détection des doublons.

Quatre critères INDÉPENDANTS (chacun produit ses propres groupes, avec la
raison du match — c'est le mode de rapport choisi) :

  1. sku              : produits partageant au moins un SKU de variante.
  2. title_normalized : titre identique après normalisation.
  3. title_vendor     : (titre normalisé, vendor normalisé) identiques.
  4. handle_fuzzy     : handles « proches ». En mode `strict`, on regroupe par
                        racine de handle (suffixe -1/-2, "copy-of-").

Dans chaque groupe, on désigne un produit « à garder » selon `dedup.keep_rule`
(ici : oldest = createdAt le plus ancien). Les autres sont « à traiter ».

NB : les groupes peuvent se chevaucher entre critères. La déduplication des
actions (ne pas archiver deux fois le même produit, ni jamais un « gardé »)
est gérée en aval (cleanup) via l'ensemble consolidé `products_to_process`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable

from .config import DedupConfig
from .normalize import handle_base, normalize_text
from .logging_setup import get_logger


@dataclass
class DuplicateGroup:
    criterion: str          # sku | title_normalized | title_vendor | handle_fuzzy
    match_key: str          # valeur normalisée qui a provoqué le regroupement
    keep: dict[str, Any]    # produit à garder
    to_process: list[dict[str, Any]]  # produits à traiter (archiver)
    reason: str             # explication lisible

    def as_rows(self) -> list[dict[str, Any]]:
        """Aplati le groupe en lignes CSV (une par produit à traiter)."""
        rows = []
        for p in self.to_process:
            rows.append(
                {
                    "criterion": self.criterion,
                    "match_key": self.match_key,
                    "reason": self.reason,
                    "keep_id": self.keep["id"],
                    "keep_legacyId": self.keep.get("legacyResourceId", ""),
                    "keep_title": self.keep.get("title", ""),
                    "keep_handle": self.keep.get("handle", ""),
                    "keep_createdAt": self.keep.get("createdAt", ""),
                    "process_id": p["id"],
                    "process_legacyId": p.get("legacyResourceId", ""),
                    "process_title": p.get("title", ""),
                    "process_handle": p.get("handle", ""),
                    "process_status": p.get("status", ""),
                    "process_createdAt": p.get("createdAt", ""),
                }
            )
        return rows


@dataclass
class DedupResult:
    groups: list[DuplicateGroup]
    # Ensemble consolidé des ids à traiter et à garder (pour cleanup).
    process_ids: set[str] = field(default_factory=set)
    keep_ids: set[str] = field(default_factory=set)

    def summary_by_criterion(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for g in self.groups:
            out[g.criterion] += len(g.to_process)
        return dict(out)


# --------------------------------------------------------------------------- #
# Règle de sélection du produit à garder
# --------------------------------------------------------------------------- #
def _keeper_selector(rule: str) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    """Retourne une fonction qui choisit le produit à garder dans un groupe."""

    def by_oldest(group: list[dict[str, Any]]) -> dict[str, Any]:
        # createdAt ISO 8601 -> tri lexicographique = tri chronologique.
        # Les valeurs nulles sont repoussées en fin (donc jamais « gardées »).
        return min(group, key=lambda p: p.get("createdAt") or "9999")

    def by_most_stock(group: list[dict[str, Any]]) -> dict[str, Any]:
        return max(group, key=lambda p: p.get("totalInventory") or -1)

    def by_most_complete(group: list[dict[str, Any]]) -> dict[str, Any]:
        def score(p: dict[str, Any]) -> tuple:
            completeness = (
                int(bool(p.get("hasImage")))
                + int((p.get("bodyHtmlLen") or 0) > 0)
                + int(bool(p.get("skus")))
                + int((p.get("collectionsCount") or 0) > 0)
            )
            # Départage par ancienneté (le plus ancien gagne) si égalité.
            return (completeness, -_created_rank(p))
        return max(group, key=score)

    return {
        "oldest": by_oldest,
        "most_stock": by_most_stock,
        "most_complete": by_most_complete,
    }.get(rule, by_oldest)


def _created_rank(p: dict[str, Any]) -> int:
    """Rang chronologique grossier pour départager (plus petit = plus ancien)."""
    val = p.get("createdAt") or ""
    # On enlève les non-chiffres pour obtenir un entier comparable.
    digits = "".join(ch for ch in val if ch.isdigit())
    return int(digits) if digits else 0


# --------------------------------------------------------------------------- #
# Construction d'un groupe à partir d'un cluster de produits
# --------------------------------------------------------------------------- #
def _make_group(
    criterion: str,
    match_key: str,
    cluster: list[dict[str, Any]],
    select_keeper: Callable[[list[dict[str, Any]]], dict[str, Any]],
    reason: str,
) -> DuplicateGroup | None:
    if len(cluster) < 2:
        return None
    keeper = select_keeper(cluster)
    to_process = [p for p in cluster if p["id"] != keeper["id"]]
    if not to_process:
        return None
    return DuplicateGroup(
        criterion=criterion,
        match_key=match_key,
        keep=keeper,
        to_process=to_process,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Critères
# --------------------------------------------------------------------------- #
def _group_by_sku(products, select_keeper) -> list[DuplicateGroup]:
    """Regroupe les produits partageant au moins un SKU.

    Un produit peut partager des SKU avec plusieurs autres : on construit des
    composantes connexes (union-find léger) pour ne pas couper des chaînes.
    """
    # SKU -> liste d'index produits
    sku_index: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(products):
        for sku in set(p.get("skus", [])):
            sku_index[sku].append(i)

    # Union-Find
    parent = list(range(len(products)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    shared_skus: dict[int, set[str]] = defaultdict(set)
    for sku, idxs in sku_index.items():
        if len(set(idxs)) < 2:
            continue
        first = idxs[0]
        for j in idxs[1:]:
            union(first, j)
        for j in idxs:
            shared_skus[find(j)].add(sku)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(products)):
        root = find(i)
        clusters[root].append(i)

    groups: list[DuplicateGroup] = []
    for root, idxs in clusters.items():
        if len(idxs) < 2:
            continue
        cluster = [products[i] for i in idxs]
        skus = sorted(shared_skus.get(root, set()))
        key = ",".join(skus) if skus else "(shared)"
        g = _make_group(
            "sku", key, cluster, select_keeper,
            reason=f"SKU partagé(s) : {key}",
        )
        if g:
            groups.append(g)
    return groups


def _group_by_key(products, key_fn, criterion, reason_fn, select_keeper) -> list[DuplicateGroup]:
    """Regroupe par clé exacte (titre normalisé, titre+vendor, racine de handle)."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in products:
        key = key_fn(p)
        if key:  # on ignore les clés vides (titre vide, etc.)
            buckets[key].append(p)

    groups: list[DuplicateGroup] = []
    for key, cluster in buckets.items():
        g = _make_group(criterion, key, cluster, select_keeper, reason_fn(key))
        if g:
            groups.append(g)
    return groups


def _group_handle_moderate(products, ratio_threshold, select_keeper) -> list[DuplicateGroup]:
    """Mode `moderate` : regroupe par similarité d'édition des handles.

    O(n²) sur les handles ; acceptable pour quelques milliers de produits.
    Pour un très gros catalogue, préférer le mode `strict`.
    """
    groups: list[DuplicateGroup] = []
    used: set[int] = set()
    handles = [(i, (p.get("handle") or "").lower()) for i, p in enumerate(products)]

    for i, (idx_i, h_i) in enumerate(handles):
        if idx_i in used or not h_i:
            continue
        cluster_idx = [idx_i]
        for idx_j, h_j in handles[i + 1:]:
            if idx_j in used or not h_j:
                continue
            if SequenceMatcher(None, h_i, h_j).ratio() >= ratio_threshold:
                cluster_idx.append(idx_j)
        if len(cluster_idx) >= 2:
            for k in cluster_idx:
                used.add(k)
            cluster = [products[k] for k in cluster_idx]
            g = _make_group(
                "handle_fuzzy", h_i, cluster, select_keeper,
                reason=f"Handles similaires (ratio >= {ratio_threshold}) à « {h_i} »",
            )
            if g:
                groups.append(g)
    return groups


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #
def detect_duplicates(products: list[dict[str, Any]], cfg: DedupConfig) -> DedupResult:
    log = get_logger()
    select_keeper = _keeper_selector(cfg.keep_rule)
    norm = cfg.normalize
    groups: list[DuplicateGroup] = []

    if cfg.criteria.get("sku"):
        g = _group_by_sku(products, select_keeper)
        log.info("Critère SKU : %d groupe(s) de doublons.", len(g))
        groups += g

    if cfg.criteria.get("title_normalized"):
        g = _group_by_key(
            products,
            key_fn=lambda p: normalize_text(p.get("title", ""), norm),
            criterion="title_normalized",
            reason_fn=lambda k: f"Titre normalisé identique : « {k} »",
            select_keeper=select_keeper,
        )
        log.info("Critère titre normalisé : %d groupe(s).", len(g))
        groups += g

    if cfg.criteria.get("title_vendor"):
        g = _group_by_key(
            products,
            key_fn=lambda p: (
                normalize_text(p.get("title", ""), norm)
                + "||"
                + normalize_text(p.get("vendor", ""), norm)
            ) if p.get("title") else "",
            criterion="title_vendor",
            reason_fn=lambda k: f"Titre+vendor identiques : « {k.replace('||', ' / ')} »",
            select_keeper=select_keeper,
        )
        log.info("Critère titre+vendor : %d groupe(s).", len(g))
        groups += g

    if cfg.criteria.get("handle_fuzzy"):
        if cfg.handle_fuzzy_mode == "moderate":
            g = _group_handle_moderate(products, cfg.handle_fuzzy_ratio, select_keeper)
        else:  # strict
            g = _group_by_key(
                products,
                key_fn=lambda p: handle_base(p.get("handle", "")),
                criterion="handle_fuzzy",
                reason_fn=lambda k: f"Handles de même racine (re-import) : « {k} »",
                select_keeper=select_keeper,
            )
        log.info("Critère handle proche (%s) : %d groupe(s).",
                 cfg.handle_fuzzy_mode, len(g))
        groups += g

    # Consolidation : ids à garder vs à traiter (pour cleanup).
    keep_ids: set[str] = {g.keep["id"] for g in groups}
    process_ids: set[str] = set()
    for grp in groups:
        for p in grp.to_process:
            process_ids.add(p["id"])

    # Garde-fou : un produit « gardé » par un critère ne doit jamais être
    # « traité » par un autre. En cas de chevauchement, on protège le keep.
    overlap = process_ids & keep_ids
    if overlap:
        log.warning(
            "%d produit(s) à la fois 'gardé' et 'à traiter' selon des critères "
            "différents — ils seront PROTÉGÉS (non traités).", len(overlap),
        )
        process_ids -= overlap

    result = DedupResult(groups=groups, process_ids=process_ids, keep_ids=keep_ids)
    log.info(
        "Dédup : %d groupe(s), %d produit(s) distinct(s) à traiter.",
        len(groups), len(process_ids),
    )
    return result
