# L3.9 — Installateur, signature, mise à jour

**Référence** : EF-10, EF-19, T-12 ; `06_L3_application_desktop.md`.

## Ce qui est fait

| Critère | Cible | Mesuré | |
|---|---|---|---|
| Installateur par utilisateur, sans droits administrateur | — | NSIS, `installMode: currentUser` | ✅ |
| Taille de l'installateur | **< 15 Mo** | **2,66 Mo** | ✅ |
| Binaire installé | — | 10,32 Mo | ✅ |
| Démarrage automatique **optionnel** | — | case à cocher, registre `HKCU` | ✅ |
| Langues de l'installateur | FR + EN | `["French", "English"]` | ✅ |

Commande de construction :

```bash
cd apps/desktop
pnpm build                      # le bundle Vue dans dist/
pnpm tauri build                # → target/release/bundle/nsis/NovaBrief_<version>_x64-setup.exe
```

Le démarrage automatique est **désactivé** tant que personne ne le demande, et
lance l'application avec `--hidden`. Il existe pour que l'enregistreur soit là
quand la réunion commence, pas pour qu'une fenêtre soit dans le passage chaque
matin ; une application qui s'installe toute seule dans le démarrage est une
application qu'on désinstalle.

## Ce qui est bloqué, et sur quoi exactement

### T-12 — l'avertissement SmartScreen (EF-10)

**Il faut un certificat de signature de code.** Sans lui, chaque installation
affiche « Windows a protégé votre ordinateur », et le taux d'abandon en PME est
élevé — la personne qui installe n'est pas celle qui a décidé d'acheter.

L'option recommandée est **Azure Trusted Signing** (~10 $/mois) plutôt qu'un
certificat EV classique (~300 $/an et un jeton matériel) :

1. Un abonnement Azure et un compte *Trusted Signing*.
2. La vérification de l'entité juridique **Novafrik** (documents d'entreprise,
   délai annoncé : quelques jours).
3. Un *certificate profile*, puis les quatre valeurs à mettre dans les secrets
   GitHub Actions : `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
   `AZURE_CLIENT_SECRET`, et l'URI du profil.

Tant que ce n'est pas fait, l'installateur fonctionne — il est simplement
affiché comme provenant d'un éditeur inconnu. **T-12 ne peut pas être prononcé.**

### EF-19 — la mise à jour silencieuse

**Il faut une paire de clés Ed25519 et un endpoint de publication.** Le plugin
`tauri-plugin-updater` n'est volontairement **pas** ajouté : il exige une clé
publique dans `tauri.conf.json`, et une configuration à moitié remplie
échouerait à la construction ou, pire, accepterait des mises à jour que
personne n'a signées.

Ce qu'il faut, dans l'ordre :

```bash
pnpm tauri signer generate -w ~/.tauri/novabrief.key
```

1. La **clé privée** et son mot de passe vont dans les secrets GitHub Actions
   (`TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`). Elle ne
   doit jamais entrer dans le dépôt : qui la détient peut faire installer
   n'importe quoi sur chaque poste client.
2. La **clé publique** va dans `tauri.conf.json`, et là elle est publique.
3. Un endpoint qui sert le `latest.json` — un fichier statique sur le domaine
   suffit ; il n'a pas besoin d'être dynamique.

**La règle d'EF-19 « jamais pendant un enregistrement » est déjà exprimable** :
`AppState::is_capturing()` répond vrai pour `Recording` **et** `Paused`, et
c'est la condition à consulter avant d'appliquer une mise à jour. Une pause
n'est pas la fin de la réunion, et redémarrer l'application la perdrait.

## Ce qui reste non prononcé

- **T-12** (SmartScreen) — dépend du certificat.
- **La mise à jour automatique** n'a jamais tourné, faute de clés.
- **Le parcours d'installation n'a pas été suivi sur une machine vierge.** Il a
  été construit et mesuré, pas installé sur un poste qui n'a jamais eu WebView2.
  C'est là que se découvre le dernier prérequis manquant.
