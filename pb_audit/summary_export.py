"""Génère deux CSV de synthèse globale (lecture seule) :

  1. resume-global-*.csv      : tableau Indicateur / Valeur (vue direction)
  2. resume-par-critere-*.csv : doublons par critère + part en %

Aucune modification de la boutique. À présenter à côté de la liste détaillée.

Usage :
    python -m pb_audit.summary_export                 # auto-détecte les derniers fichiers
    python -m pb_audit.summary_export <dedup.csv> <cache.json>
"""

from __future__ import annotations

import csv
import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

CRIT_LABELS = {
    "sku": "Même code-barres / SKU",
    "title_normalized": "Même nom de produit",
    "title_vendor": "Même nom + marque",
    "handle_fuzzy": "Lien (URL) quasi identique",
}


def _latest(pattern: str, exclude: tuple[str, ...] = ()) -> str | None:
    files = [f for f in glob.glob(pattern) if not any(e in f for e in exclude)]
    return max(files, key=os.path.getmtime) if files else None


def main(argv: list[str]) -> int:
    dedup_csv = (argv[1] if len(argv) > 1 else None) or _latest(
        "reports/dedup-*.csv",
        exclude=("summary", "review", "direction", "complete", "simple", "final", "resume"),
    )
    cache_json = (argv[2] if len(argv) > 2 else None) or _latest("cache/*.json")
    if not dedup_csv or not cache_json:
        raise SystemExit("Fichiers introuvables. Lance d'abord `python run.py --dedup`.")

    payload = json.load(open(cache_json, encoding="utf-8"))
    products = payload.get("products", [])
    cache = {p["id"]: p for p in products}
    rows = list(csv.DictReader(open(dedup_csv, encoding="utf-8-sig")))
    for r in rows:
        r["keep_status"] = cache.get(r["keep_id"], {}).get("status", "?")

    # Calculs (mêmes règles que le moteur de dédup).
    process_ids = {r["process_id"] for r in rows}
    keep_ids = {r["keep_id"] for r in rows}
    protected = process_ids & keep_ids
    process_effective = process_ids - protected

    by_crit = Counter(r["criterion"] for r in rows)
    cat_status = Counter(p.get("status", "?") for p in products)
    proc_status = Counter(
        r["process_status"] for r in rows if r["process_id"] in process_effective
    )
    danger = sum(
        1 for r in rows
        if r["process_status"] == "ACTIVE" and r["keep_status"] != "ACTIVE"
    )

    total = len(products)
    n_proc = len(process_effective)
    pct = (n_proc / total * 100) if total else 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # ---- CSV 1 : résumé global (Indicateur / Valeur) ----------------------
    p1 = Path("reports") / f"resume-global-{ts}.csv"
    indic = [
        ("Date du rapport", datetime.now().strftime("%d/%m/%Y %H:%M")),
        ("Produits au catalogue", total),
        ("Produits en ligne (ACTIVE)", cat_status.get("ACTIVE", 0)),
        ("Produits en brouillon (DRAFT)", cat_status.get("DRAFT", 0)),
        ("Produits déjà archivés", cat_status.get("ARCHIVED", 0)),
        ("Correspondances de doublons détectées", len(rows)),
        ("Doublons distincts à traiter", n_proc),
        ("Part du catalogue concernée (%)", f"{pct:.1f}"),
        ("  dont à retirer en ligne (ACTIVE)", proc_status.get("ACTIVE", 0)),
        ("  dont en brouillon (DRAFT)", proc_status.get("DRAFT", 0)),
        ("Produits protégés (non touchés par précaution)", len(protected)),
        ("Produits en ligne retirés au profit d'un brouillon", danger),
    ]
    with p1.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Indicateur", "Valeur"])
        for k, v in indic:
            w.writerow([k, v])

    # ---- CSV 2 : résumé par critère --------------------------------------
    p2 = Path("reports") / f"resume-par-critere-{ts}.csv"
    total_pairs = len(rows) or 1
    with p2.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Type de doublon", "Correspondances", "Part (%)"])
        for k in ("sku", "title_normalized", "title_vendor", "handle_fuzzy"):
            n = by_crit.get(k, 0)
            w.writerow([CRIT_LABELS[k], n, f"{n / total_pairs * 100:.1f}"])
        w.writerow(["TOTAL", len(rows), "100.0"])

    print(f"Résumé global       : {p1}")
    print(f"Résumé par critère  : {p2}")
    print(f"  Catalogue={total}  Doublons à traiter={n_proc} ({pct:.1f}%)  Dangereux={danger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
