# pb_audit — Audit & déduplication du catalogue Shopify

Outil en ligne de commande pour **auditer** un catalogue Shopify, **détecter les
doublons** et les **nettoyer proprement** via l'Admin GraphQL API.

> **Sécurité d'abord.** L'outil est en **DRY-RUN par défaut** : il détecte et
> rapporte, sans rien modifier. Aucune action destructive ne s'exécute sans le
> flag explicite `--apply`. Les doublons sont **archivés** (réversible), jamais
> supprimés — la suppression définitive est isolée derrière `--hard-delete` +
> confirmation interactive.

---

## 1. Prérequis

- Python 3.10+
- Une **app personnalisée** Shopify utilisant le grant **`client_credentials`**,
  avec les scopes `read_products` et `write_products`.

## 2. Installation

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configuration

### Secrets (jamais commités)

Copiez `.env.example` en `.env` et renseignez les vraies valeurs :

```bash
cp .env.example .env
```

```dotenv
SHOPIFY_SHOP=paul-beuscher-2.myshopify.com
SHOPIFY_CLIENT_ID=...
SHOPIFY_CLIENT_SECRET=...
```

Les secrets viennent **exclusivement** de l'environnement. Ils ne sont jamais
écrits sur disque par l'outil, ni logués (un filtre caviarde tout secret
éventuel). Le `.env` et les dossiers `cache/`, `logs/`, `reports/`, `backups/`
sont déjà dans `.gitignore`.

### Paramètres (versionnables)

Copiez `config.example.yaml` en `config.yaml` et ajustez :

```bash
cp config.example.yaml config.yaml
```

Principaux réglages (voir les commentaires du fichier pour le détail) :

| Clé | Rôle | Défaut |
|---|---|---|
| `api.version` | Version Admin GraphQL (validée au préflight) | `2026-04` |
| `cache.ttl_hours` | Fraîcheur du cache avant re-fetch | `6` |
| `cache.fetch_strategy` | `auto` / `paginate` / `bulk` | `auto` |
| `cache.bulk_threshold` | Bascule auto → Bulk Operations | `2000` |
| `dedup.criteria.*` | Critères de doublon actifs | tous |
| `dedup.handle_fuzzy_mode` | `strict` (suffixe -1/-2) / `moderate` | `strict` |
| `dedup.keep_rule` | Produit gardé : `oldest` / `most_stock` / `most_complete` | `oldest` |
| `cleanup.action` | `archive` (ARCHIVED) / `draft` | `archive` |
| `cleanup.batch_size` | Mutations par lot | `25` |

## 4. Workflow recommandé

```bash
# 1) Préflight : vérifie auth, boutique, scopes, version d'API
python run.py --test-connection

# 2) Audit qualité des données (CSV + résumé Markdown)
python run.py --audit

# 3) Détection des doublons — DRY-RUN : ne modifie RIEN, génère les rapports
python run.py --dedup

# 4) >>> REVUE MANUELLE des CSV dans reports/ et du backup dans backups/ <<<

# 5) Application : archive les doublons « à traiter »
python run.py --dedup --apply
```

`--refresh` force le re-fetch des produits (ignore le cache).
On peut combiner : `python run.py --audit --dedup`.

### Ce que produit chaque commande

- **`--test-connection`** : nom de boutique, plan, devise, version d'API,
  scopes ; échoue tôt si auth / scopes / version manquent.
- **`--audit`** : `reports/audit-*.csv`, `reports/audit-sku-duplicates-*.csv`,
  `reports/audit-summary-*.md` (SKU manquants/dupliqués, sans image, body vide,
  inventaire non suivi / stock 0, répartition par statut, sans collection).
- **`--dedup`** : `reports/dedup-*.csv` (produit gardé, produits à traiter,
  critère, raison) + `reports/dedup-summary-*.md`. Un **backup CSV** des
  produits concernés est écrit dans `backups/` avant toute action.

## 5. Critères de doublon

Les quatre critères sont **indépendants** : chacun génère ses propres groupes
dans le rapport, avec la raison du match (transparence pour la revue).

| Critère | Description |
|---|---|
| `sku` | Produits partageant au moins un SKU de variante (composantes connexes). |
| `title_normalized` | Titre identique après normalisation (minuscules, trim, accents, espaces). |
| `title_vendor` | Titre normalisé **et** vendor identiques. |
| `handle_fuzzy` | Handles de même racine (re-import : `-1`, `-2`, `copy-of-…`) en mode `strict`. |

Dans chaque groupe, le produit **gardé** suit `dedup.keep_rule` (par défaut le
plus ancien, `createdAt`). Si un produit est « gardé » par un critère et
« à traiter » par un autre, il est **protégé** (jamais traité).

## 6. Sécurité & robustesse

- **DRY-RUN par défaut** ; mutations uniquement avec `--apply`.
- **Archive, pas delete** : `cleanup.action = archive` → statut `ARCHIVED`
  (réversible). La suppression dure (`productDelete`) exige `--apply
  --hard-delete` **et** la saisie interactive de `SUPPRIMER`.
- **Backup CSV obligatoire** avant action (id, titre, handle, SKU, statut,
  inventaire).
- **Idempotent** : un produit déjà au statut cible est ignoré (`skipped`).
- **Rate limit par coût** : respect de `extensions.cost.throttleStatus` avec
  back-off exponentiel quand le budget est bas.
- **Retries** : `401` → ré-auth, `429` → `Retry-After`, `5xx` → back-off,
  `THROTTLED` GraphQL → attente de restauration.
- **Logs horodatés** par run dans `logs/`, sans jamais écrire un secret/token.

## 7. Procédure de retour arrière (rollback)

Un doublon traité a été **archivé** (`ARCHIVED`) — rien n'est supprimé. Pour
réactiver un produit :

1. Retrouvez son `id` dans le **backup CSV** (`backups/backup-before-apply-*.csv`)
   ou dans le rapport `reports/dedup-*.csv` (colonne `process_id`).
2. Réactivez-le, au choix :
   - **Admin Shopify** : ouvrez le produit → statut → *Actif*.
   - **GraphQL** (`productUpdate`) :
     ```graphql
     mutation Reactivate($product: ProductUpdateInput!) {
       productUpdate(product: $product) {
         product { id status }
         userErrors { field message }
       }
     }
     ```
     variables :
     ```json
     { "product": { "id": "gid://shopify/Product/<ID>", "status": "ACTIVE" } }
     ```

> En cas de `--hard-delete` (déconseillé), la suppression est **irréversible** :
> il n'existe pas de rollback. Le backup CSV ne permet que de retrouver les
> métadonnées, pas de recréer le produit à l'identique.

## 8. Structure du projet

```
pb_audit/
  config.py         # YAML + .env, validation
  auth.py           # client_credentials, token en mémoire, expiration
  client.py         # GraphQL : rate limit (coût), back-off, retry, ré-auth
  fetch.py          # pagination + Bulk Operations + cache
  preflight.py      # Module 1 : test-connection
  audit.py          # Module 3 : audit qualité
  dedup.py          # Module 4 : détection des doublons
  cleanup.py        # Module 5 : archivage idempotent
  reporting.py      # CSV + résumés Markdown ; backup CSV
  normalize.py      # normalisation des chaînes
  logging_setup.py  # logging structuré + caviardage des secrets
  cli.py            # point d'entrée des commandes
run.py              # wrapper d'exécution
config.example.yaml # config d'exemple
.env.example        # secrets d'exemple (placeholders)
```

## 9. Codes de sortie

| Code | Signification |
|---|---|
| `0` | Succès. |
| `1` | Erreur d'exécution / au moins une mutation en erreur / annulation hard-delete. |
| `2` | Mauvais usage : config/secret invalide, scopes/version manquants, flags incohérents. |
