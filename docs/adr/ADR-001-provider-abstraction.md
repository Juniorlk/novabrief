# ADR-001 — Aucune dépendance directe à un fournisseur d'IA ou de paiement

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 1

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

Le produit dépend de fournisseurs externes pour la transcription (AssemblyAI,
Deepgram), l'analyse (OpenAI) et l'encaissement (Flutterwave, plus tard Stripe).
Ces fournisseurs changent de tarif, de modèle et de disponibilité ; §5.8 prévoit
explicitement un basculement vers Whisper auto-hébergé quand le volume le
justifie, et §20.1 un second fournisseur de paiement pour les marchés hors CEMAC.
Si leurs SDK se répandent dans le code métier, chacun de ces changements devient
une réécriture.

## Décision

Tout appel de transcription passe par `TranscriptionProvider`, tout appel LLM
par `LLMProvider`, tout paiement par `BillingProvider` (interfaces normatives
§18.1 et §20.1). Un fournisseur se change par configuration, sans modifier le
code métier.

- Les SDK et clients HTTP des fournisseurs ne sont importés que dans
  `packages/ai/**` et dans le module de facturation qui implémente le provider.
- Les noms de modèles, les clés et l'ordre de préférence sont des variables
  d'environnement ou des données de configuration, jamais des littéraux dans le
  code métier.
- Le `TranscriptionRouter` porte le disjoncteur et le repli ; les appelants
  ignorent quel fournisseur a répondu.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Appeler les SDK directement dans les services | Chaque changement de fournisseur ou de modèle touche tout le code ; le repli et le disjoncteur deviennent impossibles à centraliser |
| Une couche d'abstraction générique tierce (LiteLLM, LangChain) | Dépendance lourde (CLAUDE.md §8), surface d'API instable, et masque les paramètres spécifiques dont on a besoin (`speaker_labels`, `multichannel`, `keyterms_prompt`) |

## Conséquences

**Positives** — le basculement vers Whisper (§5.8) et l'ajout de Stripe (V1.1)
sont des tâches locales ; les fournisseurs se simulent en test et en
environnement `dev` ; le coût par appel se mesure au même endroit (ADR-008).

**Négatives et coûts acceptés** — une couche d'indirection à maintenir, et
l'obligation de traduire les capacités spécifiques d'un fournisseur dans une
interface commune, parfois au prix du plus petit dénominateur.

## Comment on vérifie qu'elle est respectée

Règle de revue et test automatisé : aucun import de `assemblyai`, `deepgram`,
`openai`, `flutterwave` ou `stripe` en dehors de `packages/ai/**` et du module
provider de facturation. Le test parcourt l'arbre d'imports et échoue sinon.
