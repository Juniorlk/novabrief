# Tâche — POC #3 : extraction structurée (résumé, décisions, tâches) sans hallucination

**Référence** : cahier des charges §13, §18.2 à §18.6, EF-42, EF-43, test T-04, ADR-02 et ADR-03.
**Prérequis** : POC #2 validé (Go). Transcriptions diarisées des 30 enregistrements disponibles. Clé `OPENAI_API_KEY` dans `.env`. Budget ≤ 30 $.

## Objectif

Prouver que, à partir d'une transcription horodatée et diarisée, on obtient un compte rendu structuré **fidèle** : titre, participants, résumé, décisions, tâches avec responsable et échéance, chacun relié à un timestamp source, **sans jamais inventer**. La mesure principale n'est pas le rappel, c'est le taux d'hallucination, qui doit être nul.

## Jeu d'évaluation à constituer

- Les 30 transcriptions du POC #2 (sortie du fournisseur retenu), au format `TranscriptionResult`.
- Pour **15 d'entre elles** (8 FR, 7 EN), une **annotation de référence** dans `tools/datasets/reference_reports/<id>.json` : liste des décisions attendues, liste des tâches attendues (action, responsable s'il est nommé, échéance textuelle), participants, et surtout une liste de **10 formulations vagues par transcription** (« il faudrait voir », « on pourrait », « quelqu'un devrait ») qui ne doivent produire aucun élément.
- 5 transcriptions **synthétiques** courtes construites pour piéger le modèle : décision annulée plus loin dans la réunion ; tâche évoquée puis refusée ; responsable jamais nommé ; échéance ambiguë ; deux personnes du même prénom.

## Ce que tu livres

1. `packages/schemas/report.py` : `Decision`, `Task`, `MeetingReport` en Pydantic v2, conformes au §18.4 (contraintes de longueur, `confidence ≥ 0,5`, validateur : chaque `source_start_ms` tombe dans un segment existant de la transcription).
2. `packages/ai/llm/` : protocole `LLMProvider` (`extract(system, user, schema, temperature)` → objet validé + usage), driver OpenAI avec sortie JSON forcée, 3 tentatives maximum avec message de correction citant l'erreur de validation, enregistrement des tokens, de la latence et du coût.
3. `packages/ai/prompts/extract_v1.md` : le prompt système du §18.3 du cahier des charges, versionné. Toute modification crée `extract_v2.md`, jamais une édition en place.
4. `packages/ai/pipeline/analyze.py` : préparation de la transcription (fusion des micro-segments, format compact `[hh:mm:ss] Speaker A: …`), découpage en fenêtres de 60 min avec 5 min de recouvrement au-delà de 90 min et passe de consolidation, post-traitement (normalisation des échéances en date ISO à partir de la date de réunion, rapprochement des responsables avec une liste de membres fournie **après** extraction, jamais dans le prompt).
5. `tools/eval_extraction.py` : exécute le pipeline sur le jeu d'évaluation, 3 fois par transcription (le résultat doit être stable), et calcule les métriques ci-dessous. Sortie : `tools/results/extraction_eval.csv` + rapport Markdown.
6. Tests unitaires du schéma, du découpage et du post-traitement (sans appel réseau).

## Métriques et critères de succès

| # | Métrique | Définition | Cible |
|---|---|---|---|
| E1 | **Hallucination** | Éléments produits à partir des formulations vagues annotées, ou décisions / tâches absentes de la transcription | **0** sur 3 exécutions × 15 transcriptions (éliminatoire) |
| E2 | Précision décisions | Décisions correctes / décisions produites | ≥ 90 % |
| E3 | Rappel décisions | Décisions attendues retrouvées / attendues | ≥ 80 % |
| E4 | Précision tâches | Tâches correctes / tâches produites | ≥ 90 % |
| E5 | Rappel tâches | Tâches attendues retrouvées / attendues | ≥ 80 % |
| E6 | Responsables | Responsable correct quand il est nommé ; `null` quand il ne l'est pas | ≥ 95 % ; 0 responsable inventé |
| E7 | Traçabilité | `source_start_ms` renvoie bien au passage concerné (± 15 s) | ≥ 95 % |
| E8 | Stabilité | Même ensemble de décisions / tâches sur 3 exécutions (à la formulation près) | ≥ 90 % |
| E9 | Coût | Coût LLM par heure de réunion | ≤ 0,03 $ |
| E10 | Latence | P95 pour 1 h de transcription | ≤ 60 s |
| E11 | JSON valide | Taux de réponses validées par Pydantic au premier essai | ≥ 95 % |

Le **NovaBrief Score** du POC = E2 × E4 (à rapprocher plus tard du score calculé sur les validations utilisateurs). Cible ≥ 85 %.

## Comparaisons à faire (dans cet ordre, budget compris)

1. GPT-5 mini, prompt v1, température 0,1 : référence.
2. GPT-5 nano sur les seuls champs `title` et `participants` : s'il atteint la même qualité, il est retenu pour ces champs.
3. Prompt v1 en anglais vs en français sur les réunions EN : le prompt doit-il suivre la langue de la réunion ?
4. Avec vs sans fenêtre de recouvrement sur les 3 réunions les plus longues.

## Ce que tu ne fais PAS

- Pas d'audio envoyé au LLM (ADR-02), pas de « résume-moi ce fichier ».
- Pas de liste de membres dans le prompt (elle sert au rapprochement après extraction).
- Pas de fine-tuning, pas de RAG, pas de recherche sémantique : ce POC porte sur une réunion à la fois.
- Pas d'intégration à l'API ni à la base : bibliothèque + scripts.

## Compte rendu attendu

Format §9 des instructions, avec le tableau E1-E11 pour chaque comparaison, 10 exemples d'erreurs commentés (faux positifs, faux négatifs, responsables manqués), la version de prompt retenue, et la liste des cas où le modèle a hésité (confiance 0,5-0,8) pour calibrer l'affichage « à confirmer » du lecteur web.

## Décision Go / No-Go

- **Go** : E1 = 0, E2 et E4 ≥ 90 %, E6 sans responsable inventé, E9 ≤ 0,03 $.
- **Go conditionnel** : rappel (E3, E5) entre 70 et 80 % avec un plan d'amélioration du prompt.
- **No-Go** : toute hallucination persistante après deux versions de prompt. On revoit alors la stratégie (modèle plus grand sur les seules décisions, double passe extraction + vérification) avant d'écrire le SaaS.
