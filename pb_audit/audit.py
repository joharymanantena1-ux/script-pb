"""Module 3 — Audit de la qualité des données du catalogue.

Repère les problèmes courants et produit des lignes exploitables (CSV) ainsi
qu'un résumé agrégé (Markdown/HTML, via reporting.py).

Problèmes détectés par produit :
  * sku_missing       : aucune variante n'a de SKU
  * no_image          : produit sans image
  * empty_body        : description (body_html) vide
  * inventory_untracked : aucune variante suivie en stock
  * zero_stock        : suivi mais inventaire total <= 0
  * no_collection     : produit dans aucune collection

Problème transverse (catalogue) :
  * sku_duplicated    : un même SKU porté par plusieurs produits
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuditRow:
    """Une ligne d'audit par produit, avec ses drapeaux de problème."""

    id: str
    legacyResourceId: str
    title: str
    handle: str
    status: str
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "legacyResourceId": self.legacyResourceId,
            "title": self.title,
            "handle": self.handle,
            "status": self.status,
            "issues": ";".join(self.issues),
            "issue_count": len(self.issues),
        }


@dataclass
class AuditReport:
    rows: list[AuditRow]
    summary: dict[str, int]
    status_breakdown: dict[str, int]
    duplicated_skus: dict[str, list[str]]  # sku -> [product ids]
    total_products: int


def _product_issues(p: dict[str, Any]) -> list[str]:
    issues: list[str] = []

    if not any(v.get("sku") for v in p.get("variants", [])):
        issues.append("sku_missing")

    if not p.get("hasImage"):
        issues.append("no_image")

    if (p.get("bodyHtmlLen") or 0) == 0:
        issues.append("empty_body")

    variants = p.get("variants", [])
    any_tracked = any(v.get("tracked") for v in variants)
    if not any_tracked:
        issues.append("inventory_untracked")
    else:
        total_inv = p.get("totalInventory")
        if total_inv is not None and total_inv <= 0:
            issues.append("zero_stock")

    if (p.get("collectionsCount") or 0) == 0:
        issues.append("no_collection")

    return issues


def run_audit(products: list[dict[str, Any]]) -> AuditReport:
    """Calcule l'audit complet du catalogue."""
    rows: list[AuditRow] = []
    summary: dict[str, int] = defaultdict(int)
    status_breakdown: dict[str, int] = defaultdict(int)
    sku_to_products: dict[str, list[str]] = defaultdict(list)

    for p in products:
        status_breakdown[p.get("status", "UNKNOWN")] += 1

        for sku in p.get("skus", []):
            sku_to_products[sku].append(p["id"])

        issues = _product_issues(p)
        for iss in issues:
            summary[iss] += 1

        if issues:
            rows.append(
                AuditRow(
                    id=p["id"],
                    legacyResourceId=p.get("legacyResourceId", ""),
                    title=p.get("title", ""),
                    handle=p.get("handle", ""),
                    status=p.get("status", ""),
                    issues=issues,
                )
            )

    # SKU dupliqués entre PLUSIEURS produits distincts.
    duplicated = {
        sku: sorted(set(pids))
        for sku, pids in sku_to_products.items()
        if len(set(pids)) > 1
    }
    summary["sku_duplicated"] = len(duplicated)

    return AuditReport(
        rows=rows,
        summary=dict(summary),
        status_breakdown=dict(status_breakdown),
        duplicated_skus=duplicated,
        total_products=len(products),
    )
