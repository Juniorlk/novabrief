# ADR-011 — Hébergement sur OVHcloud plutôt que Hetzner

**Statut** : Accepté
**Date** : 2026-09-08
**Décideurs** : Novafrik (direction)
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.2 et §22.1 ; premier ADR hors du socle figé

## Contexte

Le cahier des charges désigne Hetzner Cloud, en instances ARM de la gamme CAX,
comme hébergeur : §15.2 le justifie par un coût environ quatre fois inférieur
à AWS ou GCP et une exploitation simple pour une équipe réduite, et §22.1
chiffre les trois paliers sur cette base (≈ 32 € par mois au palier 1, soit
environ 21 000 FCFA).

Novafrik a décidé le 2026-09-08 d'héberger sur OVHcloud. Cette décision est
antérieure à tout déploiement : aucune infrastructure Hetzner n'existe, donc il
n'y a rien à migrer.

## Décision

L'infrastructure de staging et de production est hébergée chez **OVHcloud**.

- Le déploiement reste **Docker Compose derrière Caddy**, inchangé. C'est ce
  qui rend la décision peu coûteuse : rien dans le code ni dans `infra/` ne
  dépend du fournisseur.
- PostgreSQL, Redis et l'API restent auto-hébergés sur des instances de
  calcul ; on ne bascule pas vers les bases managées d'OVHcloud dans cette
  version, pour ne pas remplacer une dépendance par une autre au milieu du MVP.
- Le stockage objet reste **Cloudflare R2** (ADR-06 et §15.2) : le choix tient
  à l'egress gratuit, que l'Object Storage d'OVHcloud ne propose pas, et les
  workers lisent l'audio à chaque traitement.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Rester sur Hetzner comme prévu au cahier des charges | Décision de Novafrik, qui relève du choix commercial du fournisseur et non d'un arbitrage technique |
| Basculer aussi le stockage objet vers OVHcloud | L'egress facturé pénalise directement le coût par heure de réunion (§5.1), que les workers font grimper en relisant l'audio |
| Bases de données managées OVHcloud | Coût mensuel supérieur à l'ensemble du palier 1, et personne n'a encore mesuré la charge réelle |

## Conséquences

**Positives** — la pile est identique, le `Caddyfile` et le `docker-compose.yml`
existants s'appliquent tels quels ; la décision n'entraîne aucune réécriture.

**Négatives et coûts acceptés** — deux points sont à revérifier avant
l'ouverture commerciale :

1. **Le modèle de coût de §22.1 est à refaire.** Les tarifs OVHcloud diffèrent
   de ceux de Hetzner, et l'offre ARM n'a pas d'équivalent direct des CAX. Les
   ≈ 32 € par mois du palier 1 ne sont plus une hypothèse valide.
2. **L'architecture des images Docker.** Le `Dockerfile` de l'API vise du
   multi-arch parce que les CAX sont en ARM. Si les instances OVHcloud
   retenues sont en x86, ce n'est plus une contrainte — mais il faut le
   trancher explicitement plutôt que de le découvrir au premier déploiement.

## Comment on vérifie qu'elle est respectée

Aucun identifiant, aucune adresse et aucun nom de région propre à un
fournisseur n'apparaît dans le code : tout passe par l'environnement
(`.env.example`). Le contrôle est le même que pour un secret — un `grep` sur
les noms de fournisseurs dans `apps/` et `packages/` doit rester vide.
