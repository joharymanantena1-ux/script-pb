"""Génération des rapports : CSV + résumés Markdown/HTML.

Tous les fichiers sont horodatés. Aucun secret n'est jamais écrit.
"""

from __future__ import annotations

import csv
import html
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .audit import AuditReport
from .dedup import DedupResult
from .logging_setup import get_logger

_ISSUE_LABELS = {
    "sku_missing": "SKU manquant",
    "no_image": "Sans image",
    "empty_body": "Description vide",
    "inventory_untracked": "Inventaire non suivi",
    "zero_stock": "Stock à 0",
    "no_collection": "Aucune collection",
    "sku_duplicated": "SKU dupliqué (multi-produits)",
}


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def write_csv(
    path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]
) -> Path:
    """Écrit une liste de dicts en CSV (UTF-8 BOM pour Excel)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    get_logger().info("CSV écrit : %s (%d lignes)", path, len(rows))
    return path


# --------------------------------------------------------------------------- #
# Rapports d'audit
# --------------------------------------------------------------------------- #
def write_audit_reports(report: AuditReport, reports_dir: str | Path) -> dict[str, Path]:
    ts = _timestamp()
    out_dir = Path(reports_dir)
    paths: dict[str, Path] = {}

    # CSV détaillé par produit à problème.
    rows = [r.as_dict() for r in report.rows]
    paths["audit_csv"] = write_csv(
        out_dir / f"audit-{ts}.csv",
        rows,
        ["id", "legacyResourceId", "title", "handle", "status",
         "issues", "issue_count"],
    )

    # CSV des SKU dupliqués entre produits.
    dup_rows = [
        {"sku": sku, "product_ids": ";".join(pids), "count": len(pids)}
        for sku, pids in sorted(report.duplicated_skus.items())
    ]
    paths["sku_dup_csv"] = write_csv(
        out_dir / f"audit-sku-duplicates-{ts}.csv",
        dup_rows,
        ["sku", "count", "product_ids"],
    )

    # Résumé Markdown.
    paths["audit_md"] = _write_audit_markdown(out_dir / f"audit-summary-{ts}.md", report)
    return paths


def _write_audit_markdown(path: Path, report: AuditReport) -> Path:
    lines = [
        "# Rapport d'audit du catalogue Shopify",
        "",
        f"- Généré le : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Produits analysés : **{report.total_products}**",
        "",
        "## Répartition par statut",
        "",
        "| Statut | Nombre |",
        "|---|---:|",
    ]
    for status, n in sorted(report.status_breakdown.items()):
        lines.append(f"| {status} | {n} |")

    lines += ["", "## Problèmes détectés", "", "| Problème | Produits concernés |",
              "|---|---:|"]
    for key, n in sorted(report.summary.items(), key=lambda kv: -kv[1]):
        label = _ISSUE_LABELS.get(key, key)
        lines.append(f"| {label} | {n} |")

    lines += [
        "",
        f"## SKU dupliqués entre produits : {len(report.duplicated_skus)}",
        "",
        "Voir le CSV `audit-sku-duplicates-*.csv` pour le détail.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    get_logger().info("Résumé Markdown écrit : %s", path)
    return path


# --------------------------------------------------------------------------- #
# Rapports de déduplication
# --------------------------------------------------------------------------- #
def write_dedup_reports(result: DedupResult, reports_dir: str | Path) -> dict[str, Path]:
    ts = _timestamp()
    out_dir = Path(reports_dir)
    paths: dict[str, Path] = {}

    rows: list[dict[str, Any]] = []
    for g in result.groups:
        rows.extend(g.as_rows())

    paths["dedup_csv"] = write_csv(
        out_dir / f"dedup-{ts}.csv",
        rows,
        ["criterion", "match_key", "reason",
         "keep_id", "keep_legacyId", "keep_title", "keep_handle", "keep_createdAt",
         "process_id", "process_legacyId", "process_title", "process_handle",
         "process_status", "process_createdAt"],
    )

    paths["dedup_md"] = _write_dedup_markdown(
        out_dir / f"dedup-summary-{ts}.md", result
    )
    return paths


def _write_dedup_markdown(path: Path, result: DedupResult) -> Path:
    by_crit = result.summary_by_criterion()
    lines = [
        "# Rapport de déduplication",
        "",
        f"- Généré le : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Groupes de doublons : **{len(result.groups)}**",
        f"- Produits distincts à traiter : **{len(result.process_ids)}**",
        "",
        "## Doublons à traiter par critère",
        "",
        "| Critère | Produits à traiter |",
        "|---|---:|",
    ]
    for crit, n in sorted(by_crit.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {crit} | {n} |")

    lines += ["", "## Aperçu des groupes (10 premiers)", ""]
    for g in result.groups[:10]:
        lines.append(
            f"- **[{g.criterion}]** garde `{g.keep.get('title','')}` "
            f"({g.keep.get('handle','')}) — {len(g.to_process)} à traiter "
            f"— {g.reason}"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    get_logger().info("Résumé dédup Markdown écrit : %s", path)
    return path


# --------------------------------------------------------------------------- #
# Backup CSV avant action destructive
# --------------------------------------------------------------------------- #
def write_backup_csv(
    products_by_id: dict[str, dict[str, Any]],
    product_ids: Iterable[str],
    backups_dir: str | Path,
) -> Path:
    """Sauvegarde l'état AVANT modification des produits concernés.

    Colonnes : id, titre, handle, SKU, statut, inventaire — comme requis.
    """
    ts = _timestamp()
    rows = []
    for pid in product_ids:
        p = products_by_id.get(pid)
        if not p:
            continue
        rows.append(
            {
                "id": p["id"],
                "legacyResourceId": p.get("legacyResourceId", ""),
                "title": p.get("title", ""),
                "handle": p.get("handle", ""),
                "skus": ";".join(p.get("skus", [])),
                "status": p.get("status", ""),
                "totalInventory": p.get("totalInventory", ""),
            }
        )
    path = Path(backups_dir) / f"backup-before-apply-{ts}.csv"
    return write_csv(
        path, rows,
        ["id", "legacyResourceId", "title", "handle", "skus",
         "status", "totalInventory"],
    )
