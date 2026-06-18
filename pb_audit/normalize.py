"""Normalisation de chaînes pour la comparaison de doublons.

Appliquée avant tout regroupement : minuscules, trim, suppression des accents,
collapse des espaces multiples — selon la config `dedup.normalize`.
"""

from __future__ import annotations

import re
import unicodedata

from .config import NormalizeConfig

_WHITESPACE_RE = re.compile(r"\s+")
# Sépare un suffixe numérique de re-import : "produit-x-1" -> ("produit-x", "1")
_HANDLE_SUFFIX_RE = re.compile(r"^(.*?)-(\d+)$")
_COPY_PREFIX_RE = re.compile(r"^copy-of-(.*)$")
# Numéro faisant partie du NOM (n°2, no 2, vol.2, tome 3, partie 4, volume 5…).
# Si le titre se termine par un tel motif, le suffixe du handle n'est PAS une
# copie de re-import : c'est une référence numérotée distincte.
_TITLE_NUMBERING_RE = re.compile(
    r"(?:n[°ºo]?|num[ée]ro|vol\.?|volume|tome|partie|part|cahier|livre|book)\s*0*(\d+)\s*$",
    re.IGNORECASE,
)


def strip_accents(text: str) -> str:
    """Retire les diacritiques (é -> e, ç -> c, etc.)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(text: str, cfg: NormalizeConfig) -> str:
    """Applique la normalisation configurée à un texte libre (titre)."""
    out = text or ""
    if cfg.strip_accents:
        out = strip_accents(out)
    if cfg.lowercase:
        out = out.lower()
    if cfg.collapse_whitespace:
        out = _WHITESPACE_RE.sub(" ", out)
    if cfg.strip:
        out = out.strip()
    return out


def title_numbering(title: str) -> str | None:
    """Retourne le numéro d'ordre si le titre se termine par n°/vol./tome N.

    Ex. "Concertino n°2" -> "2" ; "Études Vol.3" -> "3" ; "Guitare X" -> None.
    """
    m = _TITLE_NUMBERING_RE.search(title or "")
    return m.group(1) if m else None


def handle_base(handle: str, title: str = "") -> str:
    """Réduit un handle à sa « racine » pour le match strict de re-import.

    On ne retire le suffixe numérique QUE s'il s'agit d'une vraie copie de
    re-import. Si le titre est une référence numérotée (« Concertino n°2 »,
    « Études Vol.3 »), le suffixe fait partie du nom : on NE le retire PAS,
    sinon on regrouperait à tort des produits distincts (faux positif).

    Exemples :
      "rouge-a-levres"      ,                    -> "rouge-a-levres"
      "rouge-a-levres-1"    , "Rouge à lèvres"   -> "rouge-a-levres"  (copie)
      "concertino-n-2"      , "Concertino n°2"   -> "concertino-n-2"  (numéroté, gardé)
      "copy-of-rouge..."    ,                    -> "rouge..."
    """
    h = (handle or "").strip().lower()
    m = _COPY_PREFIX_RE.match(h)
    if m:
        h = m.group(1)

    m = _HANDLE_SUFFIX_RE.match(h)
    if m:
        suffix_num = m.group(2)
        # Si le titre se termine par ce même numéro d'ordre -> référence
        # numérotée distincte : on conserve le handle complet.
        if title and title_numbering(title) == str(int(suffix_num)):
            return h
        h = m.group(1)
    return h
