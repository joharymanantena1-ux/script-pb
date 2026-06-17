"""Point d'entrée CLI.

Workflow recommandé :
  1. --test-connection         (préflight)
  2. --audit                   (rapport qualité de données)
  3. --dedup                   (détection des doublons, DRY-RUN : ne modifie rien)
  4. revue des CSV générés
  5. --dedup --apply           (archive les doublons « à traiter »)

Sécurité :
  * DRY-RUN par défaut : sans --apply, aucune mutation n'est émise.
  * --apply déclenche un backup CSV AVANT toute action.
  * --hard-delete (avec --apply) active la suppression définitive, et exige une
    confirmation interactive explicite. Désactivé par défaut.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import requests

from .audit import run_audit
from .auth import AuthError, TokenProvider
from .cleanup import Cleaner
from .client import ShopifyGraphQLClient
from .config import ConfigError, load_config, load_secrets
from .dedup import detect_duplicates
from .fetch import ProductFetcher
from .logging_setup import setup_logging
from .preflight import PreflightError, run_preflight
from . import reporting


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pb_audit",
        description="Audit et déduplication du catalogue Shopify (dry-run par défaut).",
    )
    # Commandes (au moins une requise).
    p.add_argument("--test-connection", action="store_true",
                   help="Préflight : auth, infos boutique, scopes, version d'API.")
    p.add_argument("--audit", action="store_true",
                   help="Module 3 : audit qualité des données (CSV + résumé).")
    p.add_argument("--dedup", action="store_true",
                   help="Module 4 : détection des doublons (rapport CSV).")

    # Modificateurs.
    p.add_argument("--apply", action="store_true",
                   help="Exécute réellement le nettoyage (sinon DRY-RUN).")
    p.add_argument("--hard-delete", action="store_true",
                   help="Active la suppression DÉFINITIVE (exige --apply + confirmation).")
    p.add_argument("--refresh", action="store_true",
                   help="Force le re-fetch des produits (ignore le cache).")
    p.add_argument("--yes", action="store_true",
                   help="Confirme automatiquement (uniquement pour --hard-delete).")

    # Fichiers.
    p.add_argument("--config", default="config.yaml", help="Chemin du YAML de config.")
    p.add_argument("--env", default=".env", help="Chemin du fichier .env.")
    return p


def _build_client(cfg, secrets) -> ShopifyGraphQLClient:
    session = requests.Session()
    tokens = TokenProvider(secrets, session=session)
    return ShopifyGraphQLClient(tokens, cfg.api_version, session=session)


def _index_by_id(products: list[dict]) -> dict[str, dict]:
    return {p["id"]: p for p in products}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not (args.test_connection or args.audit or args.dedup):
        print("Rien à faire : précisez --test-connection, --audit et/ou --dedup.",
              file=sys.stderr)
        return 2

    # Chargement config + secrets (échec tôt et clair).
    try:
        cfg = load_config(args.config)
        secrets = load_secrets(args.env)
    except ConfigError as exc:
        print(f"[CONFIG] {exc}", file=sys.stderr)
        return 2

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log, log_file = setup_logging(
        cfg.logging.dir, cfg.logging.level, run_name="pb_audit", timestamp=ts)
    log.info("Démarrage. Boutique=%s, version API=%s", secrets.shop, cfg.api_version)
    log.info("Mode : %s", "APPLY" if args.apply else "DRY-RUN")

    client = _build_client(cfg, secrets)

    try:
        # --- Module 1 : test-connection -------------------------------
        if args.test_connection:
            run_preflight(client, cfg.api_version)
            if not (args.audit or args.dedup):
                return 0

        # Les modules 3/4/5 ont besoin des produits.
        need_products = args.audit or args.dedup
        products = []
        if need_products:
            fetcher = ProductFetcher(client, cfg, secrets.shop)
            products = fetcher.get_products(refresh=args.refresh)
            log.info("Produits chargés : %d", len(products))

        # --- Module 3 : audit -----------------------------------------
        if args.audit:
            report = run_audit(products)
            paths = reporting.write_audit_reports(report, cfg.output.reports_dir)
            log.info("Audit terminé. Rapports : %s",
                     ", ".join(str(v) for v in paths.values()))

        # --- Module 4 + 5 : dédup (+ nettoyage si --apply) ------------
        if args.dedup:
            result = detect_duplicates(products, cfg.dedup)
            paths = reporting.write_dedup_reports(result, cfg.output.reports_dir)
            log.info("Rapport dédup : %s", ", ".join(str(v) for v in paths.values()))

            if not result.process_ids:
                log.info("Aucun doublon à traiter.")
                return 0

            by_id = _index_by_id(products)

            # Backup CSV AVANT toute action (même en dry-run, utile pour revue).
            backup = reporting.write_backup_csv(
                by_id, result.process_ids, cfg.output.backups_dir)
            log.info("Backup des produits concernés : %s", backup)

            cleaner = Cleaner(client, cfg)

            if args.hard_delete:
                if not args.apply:
                    log.error("--hard-delete exige --apply.")
                    return 2
                if not _confirm_hard_delete(len(result.process_ids), args.yes):
                    log.warning("Suppression définitive annulée par l'utilisateur.")
                    return 1
                summary = cleaner.hard_delete(by_id, result.process_ids)
            else:
                summary = cleaner.run(by_id, result.process_ids, apply=args.apply)

            log.info(
                "Bilan : appliqués=%d, ignorés(idempotent)=%d, dry-run=%d, erreurs=%d",
                summary.applied, summary.skipped, summary.dry_run, summary.errors,
            )
            if not args.apply:
                log.info("DRY-RUN : aucune modification effectuée. "
                         "Relancez avec --apply après revue des CSV.")
            if summary.errors:
                return 1

        log.info("Terminé. Journal complet : %s", log_file)
        return 0

    except (AuthError, PreflightError) as exc:
        log.error("%s", exc)
        return 2
    except Exception as exc:  # filet de sécurité : on logue proprement
        log.exception("Erreur inattendue : %s", exc)
        return 1


def _confirm_hard_delete(count: int, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    prompt = (
        f"\n⚠️  SUPPRESSION DÉFINITIVE de {count} produit(s). "
        "Cette action est IRRÉVERSIBLE.\n"
        "Tapez exactement 'SUPPRIMER' pour confirmer : "
    )
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip() == "SUPPRIMER"


if __name__ == "__main__":
    raise SystemExit(main())
