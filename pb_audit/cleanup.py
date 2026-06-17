"""Module 5 — Nettoyage (s'exécute UNIQUEMENT avec --apply).

Garanties de sécurité :
  * Dry-run par défaut : sans --apply, on n'émet AUCUNE mutation, on rapporte.
  * Jamais le produit « gardé » : on n'agit que sur `process_ids`.
  * Idempotent : un produit déjà dans le statut cible est ignoré (skip).
  * Backup CSV obligatoire avant toute mutation (géré par l'appelant via
    reporting.write_backup_csv).
  * Log avant/après de chaque action.
  * Pas de suppression dure ici : seul un statut ARCHIVED/DRAFT est appliqué.
    La suppression dure (productDelete) vit dans hard_delete() et exige un
    flag séparé + confirmation interactive côté CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import GraphQLError, ShopifyGraphQLClient
from .logging_setup import get_logger

# action -> statut Shopify cible
_ACTION_TO_STATUS = {"archive": "ARCHIVED", "draft": "DRAFT"}

_PRODUCT_UPDATE = """
mutation SetStatus($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id status }
    userErrors { field message }
  }
}
"""

_PRODUCT_DELETE = """
mutation DeleteProduct($input: ProductDeleteInput!) {
  productDelete(input: $input) {
    deletedProductId
    userErrors { field message }
  }
}
"""


@dataclass
class ActionResult:
    product_id: str
    title: str
    before_status: str
    after_status: str
    outcome: str   # applied | skipped_idempotent | error | dry_run
    detail: str = ""


@dataclass
class CleanupSummary:
    results: list[ActionResult] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return sum(1 for r in self.results if r.outcome == "applied")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.outcome == "skipped_idempotent")

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.outcome == "error")

    @property
    def dry_run(self) -> int:
        return sum(1 for r in self.results if r.outcome == "dry_run")


def _chunks(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class Cleaner:
    def __init__(self, client: ShopifyGraphQLClient, cfg):
        self._client = client
        self._cfg = cfg
        self._log = get_logger()
        self._target_status = _ACTION_TO_STATUS[cfg.cleanup.action]

    def run(
        self,
        products_by_id: dict[str, dict[str, Any]],
        process_ids: set[str],
        apply: bool,
    ) -> CleanupSummary:
        """Applique (ou simule) le passage en statut cible des doublons.

        Idempotence : on saute les produits déjà au statut cible.
        """
        summary = CleanupSummary()
        target = self._target_status

        # On ne traite que des produits connus, jamais un keep (déjà exclu en amont).
        todo = [pid for pid in process_ids if pid in products_by_id]
        self._log.info(
            "%s : %d produit(s) à passer en %s.",
            "APPLY" if apply else "DRY-RUN", len(todo), target,
        )

        for batch in _chunks(todo, self._cfg.cleanup.batch_size):
            for pid in batch:
                p = products_by_id[pid]
                before = p.get("status", "")
                title = p.get("title", "")

                # Idempotence : déjà au bon statut -> skip.
                if before == target:
                    self._log.info("SKIP (déjà %s) : %s [%s]", target, title, pid)
                    summary.results.append(ActionResult(
                        pid, title, before, target, "skipped_idempotent"))
                    continue

                if not apply:
                    self._log.info(
                        "DRY-RUN : %s [%s] %s -> %s", title, pid, before, target)
                    summary.results.append(ActionResult(
                        pid, title, before, target, "dry_run"))
                    continue

                # --- Mutation réelle -----------------------------------
                self._log.info("AVANT : %s [%s] statut=%s", title, pid, before)
                try:
                    data = self._client.execute(
                        _PRODUCT_UPDATE,
                        variables={"product": {"id": pid, "status": target}},
                        estimated_cost=20,
                    )
                    payload = data["productUpdate"]
                    errs = payload.get("userErrors") or []
                    if errs:
                        self._log.error("userErrors pour %s : %s", pid, errs)
                        summary.results.append(ActionResult(
                            pid, title, before, before, "error", str(errs)))
                        continue
                    after = (payload.get("product") or {}).get("status", target)
                    self._log.info("APRÈS : %s [%s] statut=%s", title, pid, after)
                    summary.results.append(ActionResult(
                        pid, title, before, after, "applied"))
                except GraphQLError as exc:
                    self._log.error("Échec mutation %s : %s", pid, exc)
                    summary.results.append(ActionResult(
                        pid, title, before, before, "error", str(exc)))

        self._log.info(
            "Nettoyage terminé : appliqués=%d, ignorés=%d, dry-run=%d, erreurs=%d",
            summary.applied, summary.skipped, summary.dry_run, summary.errors,
        )
        return summary

    # ----------------------------------------------------------------- #
    # Suppression dure — DÉSACTIVÉE par défaut, flag séparé + confirmation.
    # ----------------------------------------------------------------- #
    def hard_delete(
        self,
        products_by_id: dict[str, dict[str, Any]],
        process_ids: set[str],
    ) -> CleanupSummary:
        """Suppression DÉFINITIVE (productDelete). Irréversible.

        N'est appelée par la CLI qu'avec --hard-delete + confirmation interactive.
        """
        summary = CleanupSummary()
        todo = [pid for pid in process_ids if pid in products_by_id]
        self._log.warning(
            "HARD-DELETE : suppression définitive de %d produit(s).", len(todo))

        for pid in todo:
            p = products_by_id[pid]
            title = p.get("title", "")
            before = p.get("status", "")
            self._log.warning("DELETE : %s [%s]", title, pid)
            try:
                data = self._client.execute(
                    _PRODUCT_DELETE,
                    variables={"input": {"id": pid}},
                    estimated_cost=20,
                )
                payload = data["productDelete"]
                errs = payload.get("userErrors") or []
                if errs:
                    summary.results.append(ActionResult(
                        pid, title, before, before, "error", str(errs)))
                    continue
                summary.results.append(ActionResult(
                    pid, title, before, "DELETED", "applied"))
            except GraphQLError as exc:
                summary.results.append(ActionResult(
                    pid, title, before, before, "error", str(exc)))
        return summary
