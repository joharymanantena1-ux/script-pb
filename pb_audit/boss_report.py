"""Génère un rapport HTML soigné (présentation direction) des doublons détectés.

Lecture seule : s'appuie sur le cache produits + le dernier rapport de dédup.
Aucune modification de la boutique. Aucun secret. Destiné à être présenté/imprimé
(ou exporté en PDF depuis le navigateur) AVANT toute action d'archivage.

Usage :
    python -m pb_audit.boss_report                 # auto-détecte les derniers fichiers
    python -m pb_audit.boss_report <dedup.csv> <cache.json>
"""

from __future__ import annotations

import csv
import glob
import html
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Chargement
# --------------------------------------------------------------------------- #
def _latest(pattern: str, exclude: tuple[str, ...] = ()) -> str | None:
    files = [f for f in glob.glob(pattern) if not any(e in f for e in exclude)]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def load_inputs(dedup_csv: str | None, cache_json: str | None):
    dedup_csv = dedup_csv or _latest(
        "reports/dedup-*.csv", exclude=("summary", "review")
    )
    cache_json = cache_json or _latest("cache/*.json")
    if not dedup_csv or not cache_json:
        raise SystemExit(
            "Fichiers introuvables. Lance d'abord `python run.py --dedup`."
        )
    rows = list(csv.DictReader(open(dedup_csv, encoding="utf-8-sig")))
    payload = json.load(open(cache_json, encoding="utf-8"))
    products = payload.get("products", [])
    cache = {p["id"]: p for p in products}
    return rows, cache, products, dedup_csv, cache_json, payload.get("fetched_at", "")


# --------------------------------------------------------------------------- #
# Agrégations
# --------------------------------------------------------------------------- #
CRIT_LABELS = {
    "sku": "SKU identique (code-barres)",
    "title_normalized": "Titre identique",
    "title_vendor": "Titre + marque identiques",
    "handle_fuzzy": "Handle de re-import (-1, -2…)",
}


def build_stats(rows: list[dict[str, Any]], cache: dict[str, dict], products: list[dict]):
    for r in rows:
        r["keep_status"] = cache.get(r["keep_id"], {}).get("status", "?")

    # Produits DISTINCTS à traiter (un produit peut apparaître dans plusieurs critères).
    process_ids = {r["process_id"] for r in rows}
    keep_ids = {r["keep_id"] for r in rows}

    by_crit = Counter(r["criterion"] for r in rows)
    process_status = Counter(r["process_status"] for r in rows)

    # Sécurité : aucun ACTIVE archivé pour garder un non-ACTIVE.
    danger = sum(
        1 for r in rows
        if r["process_status"] == "ACTIVE" and r["keep_status"] != "ACTIVE"
    )

    # Même règle que le moteur de dédup : un produit "à garder" selon un
    # critère ne peut pas être "à traiter" selon un autre -> il est protégé.
    protected = process_ids & keep_ids
    process_effective = process_ids - protected

    # Statut global du catalogue.
    cat_status = Counter(p.get("status", "?") for p in products)

    return {
        "total_products": len(products),
        "pairs": len(rows),
        "process_distinct": len(process_effective),
        "protected": len(protected),
        "keep_distinct": len(keep_ids),
        "by_crit": by_crit,
        "process_status": process_status,
        "danger": danger,
        "cat_status": cat_status,
    }


def sample_groups(rows: list[dict[str, Any]], criterion: str, n: int) -> list[dict]:
    return [r for r in rows if r["criterion"] == criterion][:n]


# --------------------------------------------------------------------------- #
# Rendu HTML
# --------------------------------------------------------------------------- #
def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _stat_card(value: str, label: str, accent: str = "") -> str:
    return f"""
      <div class="card {accent}">
        <div class="card-value">{_esc(value)}</div>
        <div class="card-label">{_esc(label)}</div>
      </div>"""


def _example_block(title: str, subtitle: str, rows: list[dict]) -> str:
    items = []
    for r in rows:
        items.append(f"""
        <div class="ex">
          <div class="ex-keep"><span class="badge keep">CONSERVÉ</span>
            <span class="st st-{_esc(r['keep_status']).lower()}">{_esc(r['keep_status'])}</span>
            <strong>{_esc(r['keep_title'])}</strong>
            <span class="meta">créé le {_esc(r['keep_createdAt'][:10])}</span>
          </div>
          <div class="ex-proc"><span class="badge proc">À ARCHIVER</span>
            <span class="st st-{_esc(r['process_status']).lower()}">{_esc(r['process_status'])}</span>
            {_esc(r['process_title'])}
            <span class="meta">créé le {_esc(r['process_createdAt'][:10])}</span>
          </div>
          <div class="ex-reason">↳ {_esc(r['reason'])}</div>
        </div>""")
    return f"""
      <div class="exblock">
        <h3>{_esc(title)}</h3>
        <p class="sub">{_esc(subtitle)}</p>
        {''.join(items)}
      </div>"""


def render_html(stats: dict, rows: list[dict], sources: tuple[str, str], fetched_at: str) -> str:
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    bc = stats["by_crit"]
    ps = stats["process_status"]
    cs = stats["cat_status"]

    crit_rows = "".join(
        f"<tr><td>{_esc(CRIT_LABELS.get(k, k))}</td>"
        f"<td class='num'>{bc.get(k, 0)}</td></tr>"
        for k in ("sku", "title_normalized", "title_vendor", "handle_fuzzy")
    )
    status_rows = "".join(
        f"<tr><td><span class='st st-{_esc(k).lower()}'>{_esc(k)}</span></td>"
        f"<td class='num'>{v}</td></tr>"
        for k, v in cs.most_common()
    )
    proc_rows = "".join(
        f"<tr><td><span class='st st-{_esc(k).lower()}'>{_esc(k)}</span></td>"
        f"<td class='num'>{v}</td></tr>"
        for k, v in ps.most_common()
    )

    examples = (
        _example_block(
            "Type 1 — Même code-barres (SKU), libellés différents",
            "Le même produit physique importé deux fois sous des noms différents.",
            sample_groups(rows, "sku", 3),
        )
        + _example_block(
            "Type 2 — Titre strictement identique",
            "Saisie ou import en double du même produit.",
            sample_groups(rows, "title_normalized", 3),
        )
        + _example_block(
            "Type 3 — Handle de re-import (suffixe -1, -2…)",
            "Signature classique d'un ré-import Shopify créant une copie.",
            sample_groups(rows, "handle_fuzzy", 3),
        )
    )

    danger = stats["danger"]
    danger_note = (
        f"<span class='ok'>✓ Aucun produit en ligne (ACTIVE) ne sera retiré "
        f"au profit d'un brouillon.</span>"
        if danger == 0
        else f"<span class='warn'>⚠ {danger} produit(s) ACTIVE seraient archivés "
        f"au profit d'un brouillon — à revoir.</span>"
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Rapport doublons — Catalogue Paul Beuscher</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         color: #1f2933; margin: 0; background: #f5f7fa; line-height: 1.5; }}
  .wrap {{ max-width: 960px; margin: 0 auto; padding: 40px 32px 64px; }}
  header {{ border-bottom: 4px solid #2b6cb0; padding-bottom: 20px; margin-bottom: 28px; }}
  header h1 {{ margin: 0 0 6px; font-size: 26px; }}
  header .sub {{ color: #52606d; font-size: 14px; }}
  h2 {{ font-size: 19px; margin: 36px 0 14px; color: #243b53; }}
  .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 18px 16px; text-align: center;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); border-top: 3px solid #cbd2d9; }}
  .card.blue {{ border-top-color: #2b6cb0; }}
  .card.amber {{ border-top-color: #d97706; }}
  .card.green {{ border-top-color: #059669; }}
  .card-value {{ font-size: 30px; font-weight: 700; color: #102a43; }}
  .card-label {{ font-size: 12px; color: #627d98; margin-top: 4px; text-transform: uppercase;
                letter-spacing: .03em; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px;
          overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th, td {{ text-align: left; padding: 11px 16px; border-bottom: 1px solid #e4e7eb; font-size: 14px; }}
  th {{ background: #f0f4f8; font-size: 12px; text-transform: uppercase; letter-spacing: .03em;
       color: #486581; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }}
  tr:last-child td {{ border-bottom: none; }}
  .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  .st {{ display: inline-block; padding: 1px 8px; border-radius: 20px; font-size: 11px;
        font-weight: 700; }}
  .st-active {{ background: #d1fae5; color: #065f46; }}
  .st-draft {{ background: #fef3c7; color: #92400e; }}
  .st-archived {{ background: #e4e7eb; color: #52606d; }}
  .exblock {{ background: #fff; border-radius: 10px; padding: 8px 20px 16px; margin-bottom: 18px;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .exblock h3 {{ font-size: 15px; margin: 16px 0 2px; color: #243b53; }}
  .exblock .sub {{ color: #627d98; font-size: 13px; margin: 0 0 12px; }}
  .ex {{ border-left: 3px solid #e4e7eb; padding: 8px 0 8px 14px; margin-bottom: 10px; }}
  .ex-keep, .ex-proc {{ font-size: 13.5px; margin: 2px 0; }}
  .ex-reason {{ font-size: 12px; color: #829ab1; margin-top: 3px; }}
  .badge {{ display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 6px;
           border-radius: 4px; margin-right: 6px; }}
  .badge.keep {{ background: #2b6cb0; color: #fff; }}
  .badge.proc {{ background: #d97706; color: #fff; }}
  .meta {{ color: #9aa5b1; font-size: 11.5px; margin-left: 6px; }}
  .callout {{ background: #ebf8ff; border: 1px solid #bee3f8; border-radius: 10px;
             padding: 16px 20px; margin: 18px 0; font-size: 14px; }}
  .ok {{ color: #065f46; font-weight: 600; }}
  .warn {{ color: #92400e; font-weight: 600; }}
  footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid #cbd2d9;
           color: #829ab1; font-size: 12px; }}
  .pill {{ background:#102a43; color:#fff; padding:2px 10px; border-radius:20px; font-size:12px; }}
  @media print {{ body {{ background: #fff; }} .card, table, .exblock {{ box-shadow: none;
    border: 1px solid #e4e7eb; }} .wrap {{ padding: 0; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Rapport d'analyse des doublons — Catalogue Paul Beuscher</h1>
    <div class="sub">Généré le {now} · Analyse en lecture seule, <strong>aucune
      modification n'a été effectuée</strong> sur la boutique.</div>
  </header>

  <h2>Synthèse</h2>
  <div class="cards">
    {_stat_card(f"{stats['total_products']:,}".replace(',', ' '), "Produits au catalogue", "blue")}
    {_stat_card(f"{stats['process_distinct']:,}".replace(',', ' '), "Doublons à traiter", "amber")}
    {_stat_card(f"{stats['pairs']:,}".replace(',', ' '), "Correspondances détectées", "")}
    {_stat_card(f"{stats['keep_distinct']:,}".replace(',', ' '), "Produits conservés", "green")}
  </div>

  <div class="callout">
    <strong>Méthode :</strong> pour chaque groupe de doublons, un produit est
    <strong>conservé</strong> (priorité au produit en ligne le plus ancien) et les
    autres sont marqués <strong>à archiver</strong> (réversible, jamais supprimés).
    <br>{danger_note}
    <br><span style="font-size:12.5px;color:#627d98;">
      {stats['protected']} produit(s) protégé(s) (à la fois conservés et candidats
      selon des critères différents) — non archivés par précaution.</span>
  </div>

  <h2>Doublons par critère de détection</h2>
  <table>
    <tr><th>Critère</th><th class="num">Correspondances</th></tr>
    {crit_rows}
  </table>
  <p style="font-size:12.5px;color:#829ab1;margin-top:8px;">
    Un même produit peut correspondre à plusieurs critères ; le total des
    correspondances ({stats['pairs']}) est donc supérieur au nombre de produits
    distincts à traiter ({stats['process_distinct']}).
  </p>

  <h2>Répartition par statut</h2>
  <div class="two">
    <div>
      <p style="font-size:13px;color:#627d98;margin:0 0 8px;">Catalogue complet</p>
      <table><tr><th>Statut</th><th class="num">Produits</th></tr>{status_rows}</table>
    </div>
    <div>
      <p style="font-size:13px;color:#627d98;margin:0 0 8px;">Produits à archiver</p>
      <table><tr><th>Statut</th><th class="num">Produits</th></tr>{proc_rows}</table>
    </div>
  </div>

  <h2>Exemples concrets de doublons</h2>
  {examples}

  <footer>
    Sources : <span class="pill">{_esc(Path(sources[0]).name)}</span>
    <span class="pill">{_esc(Path(sources[1]).name)}</span><br>
    Données catalogue récupérées le {_esc(fetched_at[:19].replace('T', ' '))} (UTC).
    La liste exhaustive est fournie dans le fichier CSV joint.
  </footer>
</div>
</body>
</html>"""


def main(argv: list[str]) -> int:
    dedup_csv = argv[1] if len(argv) > 1 else None
    cache_json = argv[2] if len(argv) > 2 else None
    rows, cache, products, dpath, cpath, fetched = load_inputs(dedup_csv, cache_json)
    stats = build_stats(rows, cache, products)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path("reports") / f"rapport-doublons-direction-{ts}.html"
    out.write_text(
        render_html(stats, rows, (dpath, cpath), fetched), encoding="utf-8"
    )
    print(f"Rapport HTML : {out}")
    print(f"  Produits catalogue        : {stats['total_products']}")
    print(f"  Doublons distincts à traiter: {stats['process_distinct']}")
    print(f"  Correspondances détectées : {stats['pairs']}")
    print(f"  Cas dangereux (ACTIVE->draft): {stats['danger']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
