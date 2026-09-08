# Charte de développement Novafrik

**Version 1.0 — Mai 2026**
*Document de référence pour tous les développeurs et intégrateurs travaillant sur novafrik-cm.com*

> Fournie par Novafrik le 2026-09-08. Elle s'applique à l'interface web (lot L4)
> et à l'interface du client desktop (lot L3). Les jetons de couleur et de
> typographie ci-dessous sont la source de vérité : ils sont déclarés une fois
> en variables CSS / configuration Tailwind, jamais recopiés en dur dans un
> composant.

---

## 1. COULEURS

### Palette principale

| Nom | HEX | Usage |
|---|---|---|
| **Bleu principal** | `#0707d3` | Boutons primaires, liens actifs, backgrounds CTA, navbar bouton contact |
| **Bleu hover** | `#1515e6` | Hover sur les boutons bleus |
| **Bleu clair** | `#2f35f5` | Variante boutons, backgrounds sections secondaires |
| **Bleu foncé** | `#13357c` | Hover sur éléments bleus foncés |
| **Jaune / Or** | `#ffd700` | Boutons secondaires, accents, back-to-top, highlights |
| **Jaune hover** | `#f5ca00` | Hover sur les éléments jaunes |
| **Jaune amber** | `#ffae00` | Variante accent, éléments de mise en valeur |

### Palette neutre

| Nom | HEX | Usage |
|---|---|---|
| **Blanc** | `#ffffff` | Backgrounds cards, texte sur fond sombre |
| **Fond doux** | `#f5f5ff` | Backgrounds sections alternées |
| **Fond bleu clair** | `#f0f4ff` | Tags, badges, highlights |
| **Gris texte** | `#6c757d` | Textes secondaires, descriptions, méta |
| **Gris foncé** | `#444444` | Textes corps de page |
| **Noir** | `#111111` | Texte principal, titres |
| **Noir pur** | `#000000` | Titres forts |

### Couleurs fonctionnelles (tags cas clients)

| Nom | HEX fond | HEX texte | Classe CSS |
|---|---|---|---|
| Bleu tag | `#e6edff` | `#4a6cf7` | `.tag-blue` |
| Orange tag | `#ffe8d9` | `#ff7a00` | `.tag-orange` |
| Jaune tag | `#fff3cd` | `#d39e00` | `.tag-yellow` |
| Rose tag | `#f8d7e8` | `#d63384` | `.tag-pink` |
| Vert tag | `#d1f4e0` | `#198754` | `.tag-green` |

### Couleurs stats (page index)

| Classe | Couleur chiffre |
|---|---|
| `.stat.yellow` | `#f6c000` |
| `.stat.blue` | `#1d2dbf` |
| `.stat.red` | `#ff5a1f` |

---

## 2. TYPOGRAPHIE

### Polices utilisées

| Police | Source | Usage |
|---|---|---|
| **Montserrat** | Google Fonts | Police principale — corps, titres, navigation, boutons |
| **Roboto** | Google Fonts | Usage secondaire ponctuel |
| **Font Awesome 5** | CDN | Icônes |

---

## Points à vérifier à l'implémentation (lots L3 et L4)

Ces points ne modifient pas la charte : ils relèvent de son application et
seront tranchés avec Novafrik au moment de construire les écrans.

- **Contraste.** Le jaune `#ffd700` sur blanc donne un rapport de contraste
  d'environ 1,6:1, très en dessous du minimum AA de 4,5:1 pour du texte. Le
  critère de sortie du lot L4 est une accessibilité ≥ 90 (test T-15) : le jaune
  devra donc porter du texte foncé, ou rester réservé aux aplats et aux accents
  non textuels.
- **Mode sombre.** La charte ne le couvre pas. L'application est un outil de
  travail utilisé en réunion ; la question se posera pour L4.
- **Chargement des polices.** Le tableau de bord doit peser moins de 500 Ko
  (critère L4) : Montserrat sera auto-hébergée en sous-ensemble plutôt que
  chargée depuis Google Fonts, ce qui évite aussi une dépendance externe sur
  une connexion camerounaise.
