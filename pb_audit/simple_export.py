"""Export CSV « langage métier » des doublons — compréhensible par tous.

Aucune notion technique (handle, SKU normalisé, critère) : tout est traduit en
phrases simples. Ajoute un niveau de confiance et signale les faux positifs
probables (volumes/numéros différents) pour qu'aucun département n'archive un
produit légitime par erreur.

Usage :
    python -m pb_audit.simple_export                 # auto-détecte les derniers fichiers
    python -m pb_audit.simple_export <dedup.csv> <cache.json>
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# Statuts Shopify -> langage clair.
STATUT_FR = {
    "ACTIVE": "En ligne (visible)",
    "DRAFT": "Brouillon (non publié)",
    "ARCHIVED": "Déjà archivé",
    "?": "Inconnu",
}

# Explication métier par type de détection.
EXPLICATION = {
    "sku": "Ces deux fiches portent le MÊME code-barres (SKU) : c'est donc "
           "le même article enregistré en double.",
    "title_normalized": "Ces deux fiches ont EXACTEMENT le même nom de produit : "
                        "c'est très probablement la même chose saisie deux fois.",
    "title_vendor": "Même nom de produit ET même marque : il s'agit du même article.",
    "handle_fuzzy": "Ces deux fiches ont une adresse web (lien) quasi identique, "
                    "signe d'un produit ré-importé en double.",
}

# Motifs trahissant des produits DISTINCTS malgré une ressemblance (faux positifs).
_VOLUME_RE = re.compile(r"\b(vol\.?\s*\d+|tome\s*\d+|n[°o]\s*\d+|part(?:ie)?\s*\d+)\b",
                        re.IGNORECASE)
_NUM_RE = re.compile(r"\d+")


def _latest(pattern: str, exclude: tuple[str, ...] = ()) -> str | None:
    files = [f for f in glob.glob(pattern) if not any(e in f for e in exclude)]
    return max(files, key=os.path.getmtime) if files else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _looks_like_false_positive(keep_title: str, proc_title: str, criterion: str) -> str:
    """Retourne une alerte si les deux titres semblent désigner des produits différents.

    On ne lève l'alerte que pour les critères « ressemblance » (titre/handle),
    jamais pour le SKU (code-barres identique = même article, fiable).
    """
    if criterion in ("sku",):
        return ""
    k, p = keep_title or "", proc_title or ""

    # 1) Mentions de volume/tome/numéro différentes -> produits distincts.
    kv = {m.group(0).lower().replace(" ", "") for m in _VOLUME_RE.finditer(k)}
    pv = {m.group(0).lower().replace(" ", "") for m in _VOLUME_RE.finditer(p)}
    if (kv or pv) and kv != pv:
        return "À VÉRIFIER : volumes/numéros différents (peut être 2 produits distincts)"

    # 2) Nombres présents différents (références, tailles) -> à vérifier.
    kn = set(_NUM_RE.findall(k))
    pn = set(_NUM_RE.findall(p))
    if kn and pn and kn != pn and not (kn & pn):
        return "À VÉRIFIER : références/chiffres différents dans les noms"

    return ""


def _confiance(criterion: str, alerte: str) -> str:
    if alerte:
        return "À vérifier"
    return {
        "sku": "Élevé",
        "title_normalized": "Élevé",
        "title_vendor": "Élevé",
        "handle_fuzzy": "Moyen",
    }.get(criterion, "Moyen")


def main(argv: list[str]) -> int:
    dedup_csv = (argv[1] if len(argv) > 1 else None) or _latest(
        "reports/dedup-*.csv", exclude=("summary", "review", "direction", "complete", "simple")
    )
    cache_json = (argv[2] if len(argv) > 2 else None) or _latest("cache/*.json")
    if not dedup_csv or not cache_json:
        raise SystemExit("Fichiers introuvables. Lance d'abord `python run.py --dedup`.")

    cache = {p["id"]: p for p in json.load(open(cache_json, encoding="utf-8"))["products"]}
    rows = list(csv.DictReader(open(dedup_csv, encoding="utf-8-sig")))
    for r in rows:
        r["keep_status"] = cache.get(r["keep_id"], {}).get("status", "?")

    # Tri : confiance Élevé d'abord, puis par nom du produit conservé.
    def conf_order(r):
        a = _looks_like_false_positive(r["keep_title"], r["process_title"], r["criterion"])
        return ({"Élevé": 0, "Moyen": 1, "À vérifier": 2}[_confiance(r["criterion"], a)],
                _norm(r["keep_title"]))
    rows.sort(key=conf_order)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path("reports") / f"doublons-explication-simple-{ts}.csv"

    cols = [
        "N°",
        "Niveau de confiance",
        "Pourquoi ce sont des doublons",
        "Produit à GARDER",
        "État du produit gardé",
        "Pourquoi on garde celui-ci",
        "Produit à RETIRER (mettre de côté)",
        "État du produit à retirer",
        "Point d'attention",
    ]
    n_alertes = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for i, r in enumerate(rows, 1):
            alerte = _looks_like_false_positive(
                r["keep_title"], r["process_title"], r["criterion"]
            )
            if alerte:
                n_alertes += 1
            confiance = _confiance(r["criterion"], alerte)
            keep_date = (r.get("keep_createdAt") or "")[:10]
            proc_date = (r.get("process_createdAt") or "")[:10]
            pourquoi_garder = (
                f"Créée le {keep_date}"
                + (" et déjà en ligne" if r["keep_status"] == "ACTIVE" else "")
                + (f" — l'autre date du {proc_date}" if proc_date else "")
            )
            w.writerow([
                i,
                confiance,
                EXPLICATION.get(r["criterion"], "Fiches très similaires."),
                r["keep_title"],
                STATUT_FR.get(r["keep_status"], r["keep_status"]),
                pourquoi_garder,
                r["process_title"],
                STATUT_FR.get(r["process_status"], r["process_status"]),
                alerte or "Aucun — doublon clair",
            ])

    print(f"CSV « langage métier » : {path}")
    print(f"  Lignes              : {len(rows)}")
    print(f"  Points d'attention  : {n_alertes} (à vérifier avant retrait)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
