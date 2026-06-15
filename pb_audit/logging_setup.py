"""Configuration du logging structuré.

Deux sorties : console (niveau configurable) + fichier horodaté par run.
Un filtre de rédaction remplace tout secret ou token qui transiterait
accidentellement par un message de log par `***REDACTED***`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

# Motifs de secrets à caviarder dans les logs, par sécurité défensive.
# (En usage normal, aucun secret ne devrait atteindre un message de log ;
#  ce filtre est une protection de dernier recours.)
_REDACT_PATTERNS = [
    # Header d'accès Shopify
    re.compile(r"(X-Shopify-Access-Token\s*[:=]\s*)(\S+)", re.IGNORECASE),
    # access_token dans une réponse JSON / une chaîne
    re.compile(r'(["\']?access_token["\']?\s*[:=]\s*["\']?)([A-Za-z0-9._\-]+)'),
    re.compile(r'(["\']?client_secret["\']?\s*[:=]\s*["\']?)([A-Za-z0-9._\-]+)'),
    re.compile(r'(["\']?client_id["\']?\s*[:=]\s*["\']?)([A-Za-z0-9._\-]+)'),
    # Jetons d'accès Shopify (préfixe shpat_/shpca_/shppa_)
    re.compile(r"\b(shp(?:at|ca|pa|ss)_)[A-Za-z0-9]+"),
]

_REDACTED = "***REDACTED***"


class RedactSecretsFilter(logging.Filter):
    """Caviarde les secrets éventuels dans le message formaté."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = msg
        for pat in _REDACT_PATTERNS:
            if pat.groups >= 2:
                redacted = pat.sub(rf"\g<1>{_REDACTED}", redacted)
            else:
                redacted = pat.sub(rf"\g<1>{_REDACTED}", redacted)
        if redacted != msg:
            # On remplace le message d'origine par sa version caviardée.
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(
    log_dir: str | Path,
    console_level: str = "INFO",
    run_name: str = "run",
    timestamp: str | None = None,
) -> tuple[logging.Logger, Path]:
    """Configure le logger racine `pb_audit`.

    Args:
        log_dir: répertoire des fichiers de log.
        console_level: niveau de la sortie console.
        run_name: préfixe du nom de fichier de log.
        timestamp: horodatage (sinon, calculé maintenant).

    Returns:
        (logger, chemin_du_fichier_de_log)
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = log_dir / f"{run_name}-{ts}.log"

    logger = logging.getLogger("pb_audit")
    logger.setLevel(logging.DEBUG)  # capte tout ; les handlers filtrent
    logger.handlers.clear()
    logger.propagate = False

    redact = RedactSecretsFilter()
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Fichier : toujours DEBUG (trace complète du run).
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(redact)
    logger.addHandler(fh)

    # Console : niveau configurable.
    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    ch.setFormatter(fmt)
    ch.addFilter(redact)
    logger.addHandler(ch)

    logger.debug("Logging initialisé. Fichier de log : %s", log_file)
    return logger, log_file


def get_logger() -> logging.Logger:
    """Retourne le logger applicatif (à appeler après setup_logging)."""
    return logging.getLogger("pb_audit")
