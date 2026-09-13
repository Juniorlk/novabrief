# uploader

Sends a recording from the local vault to Cloudflare R2, and survives the
network on the way (EF-18, section 16.4).

## Ce que ce crate garantit

**Un téléversement peut être interrompu n'importe où et repris depuis le
disque**, y compris après la mort du processus. C'est la conséquence directe
de l'ADR-05 : le réseau est optionnel pendant la réunion, donc au moment où
quoi que ce soit part, l'audio existe déjà et le perdre n'est plus acceptable.

## Ce qu'il ne fait pas

**Il n'attend pas.** `Uploader::attempt` fait ce que le réseau permet et dit ce
qui l'a arrêté ; c'est l'appelant qui décide quand réessayer, avec `Backoff`.

Deux raisons. La politique « réessayer sans limite, jusqu'à 5 min d'écart »
reste à un seul endroit visible. Et toute la résilience — perdre le réseau à
mi-chemin, y revenir, ne rien renvoyer deux fois — se teste **sans dormir et
sans serveur**.

## La forme

| | |
|---|---|
| `assembly` | le coffre chiffré → les octets, en flux, jamais assemblés en entier |
| `progress` | ce qui survit à un redémarrage : `upload.json`, à côté de l'enregistrement |
| `backoff` | 1 s, doublé, plafonné à 5 min, sans limite de tentatives |
| `Transport` | ce que le téléverseur attend du réseau |

`Transport` est un trait plutôt que le client lui-même : avec un vrai client
HTTP en travers, « la troisième partie a échoué et les deux premières n'ont pas
été renvoyées » ne se vérifierait qu'en débranchant un câble.

## Ce que l'assemblage ne fait jamais

Il n'écrit pas le fichier reconstitué sur le disque. Une réunion de 4 h fait
56 Mo, et l'écrire pour le téléverser mettrait de l'audio en clair sur le
disque — la seule chose qu'EF-17 interdit.

Deux passes en flux : une pour mesurer (taille + SHA-256), une pour remettre
les parties. Chacune ne tient qu'un segment et au plus une partie à la fois.
