"""Export CSV final compact — une ligne par paire de doublons.

Colonnes (format demandé, peu de texte) :
  Link1 | SKU1 | EAN1 | Nom1 | Statut1 |
  Link2 | SKU2 | EAN2 | Nom2 | Statut2 | Raison | Conserver

  * LinkN : URL admin Shopify de la fiche produit.
  * SKUN  : référence interne (champ sku de la 1re variante).
  * EANN  : code-barres (champ barcode de la 1re variante), distinct du SKU.
  * Produit 1 = à conserver ; Produit 2 = à retirer. "Conserver" = "Produit 1".

Usage :
    python -m pb_audit.final_export                 # auto-détecte derniers fichiers
    python -m pb_audit.final_export <dedup.csv> <cache.json>
"""

from __future__ import annotations

import csv
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

RAISON = {
    "sku": "Même SKU",
    "title_normalized": "Même nom",
    "title_vendor": "Même nom+marque",
    "handle_fuzzy": "Lien quasi identique",
}


def _latest(pattern: str, exclude: tuple[str, ...] = ()) -> str | None:
    files = [f for f in glob.glob(pattern) if not any(e in f for e in exclude)]
    return max(files, key=os.path.getmtime) if files else None


def _shop_domain() -> str:
    # Lecture directe du .env, sans dépendre d'un chargement de secrets complet.
    for line in Path(".env").read_text(encoding="utf-8").splitlines() if Path(".env").is_file() else []:
        if line.startswith("SHOPIFY_SHOP="):
            return line.split("=", 1)[1].strip()
    return "votre-boutique.myshopify.com"


def main(argv: list[str]) -> int:
    dedup_csv = (argv[1] if len(argv) > 1 else None) or _latest(
        "reports/dedup-*.csv",
        exclude=("summary", "review", "direction", "complete", "simple", "final", "resume"),
    )
    cache_json = (argv[2] if len(argv) > 2 else None) or _latest("cache/*.json")
    if not dedup_csv or not cache_json:
        raise SystemExit("Fichiers introuvables. Lance d'abord `python run.py --dedup`.")

    shop = _shop_domain()
    cache = {p["id"]: p for p in json.load(open(cache_json, encoding="utf-8"))["products"]}
    rows = list(csv.DictReader(open(dedup_csv, encoding="utf-8-sig")))

    def fields(pid: str):
        p = cache.get(pid, {})
        skus = p.get("skus") or []
        barcodes = p.get("barcodes") or []
        lid = p.get("legacyResourceId", "")
        link = f"https://{shop}/admin/products/{lid}" if lid else ""
        return link, (skus[0] if skus else ""), (barcodes[0] if barcodes else ""), p.get("status", "")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path("reports") / f"rapport-final-doublons-{ts}.csv"
    cols = ["Link1", "SKU1", "EAN1", "Nom1", "Statut1",
            "Link2", "SKU2", "EAN2", "Nom2", "Statut2", "Raison", "Conserver"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            l1, s1, e1, st1 = fields(r["keep_id"])      # à conserver
            l2, s2, e2, st2 = fields(r["process_id"])   # à retirer
            w.writerow([l1, s1, e1, r["keep_title"], st1,
                        l2, s2, e2, r["process_title"], st2,
                        RAISON.get(r["criterion"], r["criterion"]), "Produit 1"])

    print(f"Rapport final : {path}  ({len(rows)} lignes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
