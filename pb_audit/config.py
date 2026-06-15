"""Chargement et validation de la configuration.

Deux sources, strictement séparées :
  * Les SECRETS (shop, client_id, client_secret) viennent EXCLUSIVEMENT des
    variables d'environnement (fichier .env non commité). Ils ne sont jamais
    écrits sur disque par l'outil et ne transitent jamais par le YAML.
  * Les PARAMÈTRES non sensibles (critères de dédup, TTL du cache, etc.)
    viennent du fichier YAML, qui peut être versionné sans risque.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Erreur de configuration (manquante, invalide, secret absent)."""


# --------------------------------------------------------------------------- #
# Secrets (depuis l'environnement uniquement)
# --------------------------------------------------------------------------- #
@dataclass
class Secrets:
    """Identifiants Shopify. Gardés en mémoire, jamais sérialisés."""

    shop: str
    client_id: str
    client_secret: str

    def __repr__(self) -> str:  # évite toute fuite accidentelle dans les logs
        return (
            f"Secrets(shop={self.shop!r}, client_id=***, client_secret=***)"
        )

    @property
    def base_url(self) -> str:
        """URL de base de l'Admin API, sans version."""
        return f"https://{self.shop}"


def load_secrets(env_file: str | os.PathLike[str] | None = ".env") -> Secrets:
    """Charge les secrets depuis le .env / l'environnement.

    Échoue tôt et clairement si l'un des trois est absent.
    """
    if env_file and Path(env_file).is_file():
        load_dotenv(env_file, override=False)

    shop = os.getenv("SHOPIFY_SHOP", "").strip()
    client_id = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()

    missing = [
        name
        for name, val in (
            ("SHOPIFY_SHOP", shop),
            ("SHOPIFY_CLIENT_ID", client_id),
            ("SHOPIFY_CLIENT_SECRET", client_secret),
        )
        if not val
    ]
    if missing:
        raise ConfigError(
            "Variables d'environnement manquantes : "
            + ", ".join(missing)
            + ". Copiez .env.example en .env et renseignez les valeurs."
        )

    # Normalise le domaine : on enlève un éventuel schéma ou slash final.
    shop = shop.replace("https://", "").replace("http://", "").rstrip("/")
    if not shop.endswith(".myshopify.com"):
        raise ConfigError(
            f"SHOPIFY_SHOP doit être un domaine .myshopify.com (reçu: {shop!r})."
        )

    return Secrets(shop=shop, client_id=client_id, client_secret=client_secret)


# --------------------------------------------------------------------------- #
# Paramètres non sensibles (depuis le YAML)
# --------------------------------------------------------------------------- #
@dataclass
class NormalizeConfig:
    lowercase: bool = True
    strip: bool = True
    strip_accents: bool = True
    collapse_whitespace: bool = True


@dataclass
class DedupConfig:
    criteria: dict[str, bool] = field(
        default_factory=lambda: {
            "sku": True,
            "title_normalized": True,
            "title_vendor": True,
            "handle_fuzzy": True,
        }
    )
    handle_fuzzy_mode: str = "strict"   # strict | moderate
    handle_fuzzy_ratio: float = 0.90
    normalize: NormalizeConfig = field(default_factory=NormalizeConfig)
    keep_rule: str = "oldest"           # oldest | most_stock | most_complete


@dataclass
class CacheConfig:
    dir: str = "cache"
    ttl_hours: float = 6.0
    fetch_strategy: str = "auto"        # auto | paginate | bulk
    bulk_threshold: int = 2000


@dataclass
class CleanupConfig:
    action: str = "archive"             # archive | draft
    batch_size: int = 25


@dataclass
class LoggingConfig:
    dir: str = "logs"
    level: str = "INFO"


@dataclass
class OutputConfig:
    reports_dir: str = "reports"
    backups_dir: str = "backups"


@dataclass
class Config:
    api_version: str = "2026-04"
    cache: CacheConfig = field(default_factory=CacheConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    # Champs valides pour la validation
    _VALID_FETCH = ("auto", "paginate", "bulk")
    _VALID_FUZZY = ("strict", "moderate")
    _VALID_KEEP = ("oldest", "most_stock", "most_complete")
    _VALID_ACTION = ("archive", "draft")

    def validate(self) -> None:
        """Vérifie la cohérence des valeurs ; lève ConfigError sinon."""
        if self.cache.fetch_strategy not in self._VALID_FETCH:
            raise ConfigError(
                f"cache.fetch_strategy invalide: {self.cache.fetch_strategy!r} "
                f"(attendu: {self._VALID_FETCH})"
            )
        if self.dedup.handle_fuzzy_mode not in self._VALID_FUZZY:
            raise ConfigError(
                f"dedup.handle_fuzzy_mode invalide: {self.dedup.handle_fuzzy_mode!r} "
                f"(attendu: {self._VALID_FUZZY})"
            )
        if self.dedup.keep_rule not in self._VALID_KEEP:
            raise ConfigError(
                f"dedup.keep_rule invalide: {self.dedup.keep_rule!r} "
                f"(attendu: {self._VALID_KEEP})"
            )
        if self.cleanup.action not in self._VALID_ACTION:
            raise ConfigError(
                f"cleanup.action invalide: {self.cleanup.action!r} "
                f"(attendu: {self._VALID_ACTION})"
            )
        if self.cleanup.batch_size < 1:
            raise ConfigError("cleanup.batch_size doit être >= 1.")
        if self.cache.ttl_hours < 0:
            raise ConfigError("cache.ttl_hours doit être >= 0.")
        if not any(self.dedup.criteria.values()):
            raise ConfigError("Au moins un critère de dédup doit être activé.")


def _get(d: dict[str, Any], key: str, default: Any) -> Any:
    """Lecture tolérante : retourne le défaut si la clé est absente ou None."""
    val = d.get(key, default)
    return default if val is None else val


def load_config(path: str | os.PathLike[str] = "config.yaml") -> Config:
    """Charge le YAML de config. Si le fichier est absent, utilise les défauts."""
    raw: dict[str, Any] = {}
    p = Path(path)
    if p.is_file():
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    elif str(path) != "config.yaml":
        # L'utilisateur a explicitement pointé un fichier qui n'existe pas.
        raise ConfigError(f"Fichier de config introuvable : {path}")

    api = _get(raw, "api", {})
    cache = _get(raw, "cache", {})
    dedup = _get(raw, "dedup", {})
    norm = _get(dedup, "normalize", {})
    cleanup = _get(raw, "cleanup", {})
    logging_ = _get(raw, "logging", {})
    output = _get(raw, "output", {})

    cfg = Config(
        api_version=str(_get(api, "version", "2026-04")),
        cache=CacheConfig(
            dir=_get(cache, "dir", "cache"),
            ttl_hours=float(_get(cache, "ttl_hours", 6.0)),
            fetch_strategy=_get(cache, "fetch_strategy", "auto"),
            bulk_threshold=int(_get(cache, "bulk_threshold", 2000)),
        ),
        dedup=DedupConfig(
            criteria={
                "sku": bool(_get(_get(dedup, "criteria", {}), "sku", True)),
                "title_normalized": bool(
                    _get(_get(dedup, "criteria", {}), "title_normalized", True)
                ),
                "title_vendor": bool(
                    _get(_get(dedup, "criteria", {}), "title_vendor", True)
                ),
                "handle_fuzzy": bool(
                    _get(_get(dedup, "criteria", {}), "handle_fuzzy", True)
                ),
            },
            handle_fuzzy_mode=_get(dedup, "handle_fuzzy_mode", "strict"),
            handle_fuzzy_ratio=float(_get(dedup, "handle_fuzzy_ratio", 0.90)),
            normalize=NormalizeConfig(
                lowercase=bool(_get(norm, "lowercase", True)),
                strip=bool(_get(norm, "strip", True)),
                strip_accents=bool(_get(norm, "strip_accents", True)),
                collapse_whitespace=bool(_get(norm, "collapse_whitespace", True)),
            ),
            keep_rule=_get(dedup, "keep_rule", "oldest"),
        ),
        cleanup=CleanupConfig(
            action=_get(cleanup, "action", "archive"),
            batch_size=int(_get(cleanup, "batch_size", 25)),
        ),
        logging=LoggingConfig(
            dir=_get(logging_, "dir", "logs"),
            level=_get(logging_, "level", "INFO"),
        ),
        output=OutputConfig(
            reports_dir=_get(output, "reports_dir", "reports"),
            backups_dir=_get(output, "backups_dir", "backups"),
        ),
    )
    cfg.validate()
    return cfg
