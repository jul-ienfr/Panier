# Performance de la collecte drive — avant/après Phase 2

## Changements livrés

1. **Parallélisation entre stores** (`panier/collector.py`) : un worker
   ThreadPoolExecutor par store, dédupliqué (jamais deux workers sur le même
   store/profil). Agrégation dans l'ordre demandé ; échec/timeout d'un store
   jamais propagé aux autres. Timeout global du cycle (défaut 300s,
   `PANIER_COLLECT_TIMEOUT_SECONDS` / `--collect-timeout-seconds`).
2. **Cache offres TTL** (`panier/offers_cache.py`) :
   `<data_dir>/cache/offers/<store>-<hash-liste>.yaml`, TTL défaut 6h
   (`PANIER_CACHE_TTL_HOURS` / `--cache-ttl-hours`). Cache hit frais = zéro
   requête réseau. Entrée stale = repli si recollecte échoue, marquée
   `stale: true` avec `age_s` dans le payload `--collect-output`.
3. **Anti-hang navigateur** : chaque invocation Managed Browser est bornée
   (`PANIER_BROWSER_COMMAND_TIMEOUT`, défaut 120s).

## Mesure simulée (scripts/bench_collect.py)

3 stores, latence réseau/anti-bot simulée à 6 s par store :

| Cycle      | Temps mur |
|------------|-----------|
| Séquentiel | 18.00 s   |
| Parallèle  | 6.00 s    |

Gain mesuré : **x3.00** (≈ nombre de stores), conforme à la théorie d'un
cycle borné par le store le plus lent.

## Reproduction live

Avec le daemon Managed Browser lancé et les profils `courses` /
`courses-auchan` disponibles :

```bash
time panier plan --meals 3 --use-pantry \
  --collect leclerc,auchan,intermarche \
  --collect-output /tmp/offers-avant.yaml --no-cache   # comportement séquentiel historique : git checkout <ref avant Phase 2>
time panier plan --meals 3 --use-pantry \
  --collect leclerc,auchan,intermarche \
  --collect-output /tmp/offers-apres.yaml              # parallèle + cache
time panier plan --meals 3 --use-pantry \
  --collect leclerc,auchan,intermarche                 # 2e run : cache hit, zéro navigateur
```

Les chiffres live dépendent de l'état anti-bot du moment (DataDome Leclerc,
challenge Intermarché) ; consigner ici chaque run de référence.
