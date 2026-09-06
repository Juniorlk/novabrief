**NOVABRIEF**

Cahier des charges complet

Cadrage stratégique · Modèle économique et projections · Spécifications fonctionnelles · Spécifications techniques

|                    |                                                                                   |
|--------------------|-----------------------------------------------------------------------------------|
| **Référence**      | NVK-CDC-NB-2026-V1.0                                                              |
| **Produit**        | NovaBrief — assistant intelligent de réunions                                     |
| **Éditeur**        | Novafrik                                                                          |
| **Nature**         | SaaS B2B multi-tenant + application desktop Windows                               |
| **Marché initial** | Cameroun (XAF, Mobile Money), puis Afrique francophone et anglophone, puis Europe |
| **Version / date** | 1.0 — septembre 2026                                                              |
| **Statut**         | Référence pour implémentation (remplace le CDC v1.0 et la Master Specification)   |
| **Diffusion**      | Confidentiel — direction Novafrik, agent IA de développement                      |

*Documents joints : NovaBrief_Modele_Financier_v1.xlsx (modèle économique complet, formules vivantes).*

*Historique : v1.0 (09/2026) — consolidation et correction du CDC v1.0 et de la Master Specification NVK-SPECS-NB-2026-FULL-V1.0 ; vérification des tarifs fournisseurs au 06/09/2026.*

**Sommaire**

*(Dans Word : clic droit sur le sommaire → « Mettre à jour les champs » si les numéros de page n'apparaissent pas.)*

0\. Synthèse exécutive

NovaBrief est un SaaS B2B édité par Novafrik qui transforme automatiquement une réunion (Teams, Meet, Zoom, appel ou réunion physique captée par le PC) en un compte rendu structuré : résumé, décisions, tâches avec responsables et échéances, transcription horodatée. Le client est une application Windows légère qui enregistre localement le micro et l'audio système, puis un pipeline cloud (transcription, diarisation, analyse LLM) produit le rapport consultable sur une interface web.

Le produit est conçu pour le marché camerounais puis africain francophone et anglophone : prix en FCFA par organisation (et non par utilisateur), paiement Mobile Money, fonctionnement hors connexion pendant la réunion, faible consommation de bande passante, puis extension vers l'Europe avec la même base de code.

<table>
<colgroup>
<col style="width: 25%" />
<col style="width: 25%" />
<col style="width: 25%" />
<col style="width: 25%" />
</colgroup>
<tbody>
<tr class="odd">
<td><p><strong>151 FCFA</strong></p>
<p>coût variable par heure de réunion traitée (≈ 0,27 $)</p></td>
<td><p><strong>59 – 72 %</strong></p>
<p>marge brute par plan à 70 % d'utilisation, prix HT</p></td>
<td><p><strong>M9 (juin 2027)</strong></p>
<p>premier mois rentable (hors salaires fondateurs)</p></td>
<td><p><strong>≈ 1,5 M FCFA</strong></p>
<p>besoin de trésorerie maximal avant le point mort</p></td>
</tr>
</tbody>
</table>

**Ce que ce document tranche.** Il consolide la réflexion menée avec les agents (v1.0 et Master Specification), corrige les incohérences relevées (deux grilles tarifaires, heures mal calculées dans le P&L à 100 organisations, taux de change à 600 FCFA/USD au lieu de 565, contrainte « coût technique ≤ 12 % » incompatible avec la grille retenue) et fixe une référence unique pour l'implémentation : la grille tarifaire A (5 000 / 10 000 / 25 000 FCFA), un modèle économique vérifié avec les tarifs API de septembre 2026 et une spécification fonctionnelle et technique prête à être exécutée par l'agent IA de développement.

**Ce qu'il faut retenir du modèle économique.** L'unité économique n'est pas le client mais l'heure de réunion traitée : elle coûte environ 151 FCFA et se vend entre 333 et 500 FCFA selon le plan. Le SaaS est rentable dès quelques dizaines de clients parce que les coûts fixes sont très faibles (développement par agent IA, infrastructure Hetzner à ~21 000 FCFA/mois au départ). En revanche, trois points structurent la rentabilité réelle : la TVA de 19,25 % qui s'applique dès que le chiffre d'affaires dépasse 50 M FCFA/an (−16 % de revenu net), le churn involontaire lié à l'absence de prélèvement automatique en Mobile Money, et le coût des comptes gratuits. À 1 000 organisations, le modèle dégage environ 4,4 M FCFA d'EBITDA mensuel (51 % du CA HT) avant rémunération des fondateurs.

**Ce qui peut faire échouer le projet.** Dans l'ordre : (1) une capture audio Windows non fiable (micro + audio système synchronisés) ; (2) une extraction IA qui invente des décisions ou des tâches, ce qui détruit la confiance ; (3) un renouvellement Mobile Money non maîtrisé ; (4) une acquisition client plus chère que prévu. Les trois POC de la phase 0 (audio, transcription FR/EN avec accents, extraction structurée) sont des portes de sortie explicites : si l'un échoue, on ne construit pas le SaaS.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr class="odd">
<td><p><strong>DÉCISION À PRENDRE —</strong> Trois décisions sont demandées à Novafrik avant le démarrage du développement :</p>
<p>1. Valider la grille tarifaire A et la règle « sans report des heures non consommées » (section 5.2).</p>
<p>2. Valider la politique de conservation audio à 30 jours par défaut (levier de coût, de sécurité et de conformité).</p>
<p>3. Valider le lancement Cameroun-only pendant 12 mois avant toute ouverture d'un second pays (section 6).</p></td>
</tr>
</tbody>
</table>

**PARTIE I — CADRAGE STRATÉGIQUE ET MODÈLE ÉCONOMIQUE**

*Cette partie s'adresse aux décideurs : elle explique pourquoi NovaBrief existe, à qui il s'adresse, comment il gagne de l'argent et à quelles conditions. Toutes les valeurs chiffrées proviennent du modèle financier Excel joint (NovaBrief_Modele_Financier_v1.xlsx), qui reste la référence vivante.*

1\. Contexte et problème à résoudre

1.1 Le problème

Les petites équipes se réunissent plusieurs fois par semaine pour coordonner, décider et répartir des actions. La restitution repose sur une prise de notes manuelle dont la chaîne est fragile : réunion → notes partielles → rédaction différée → compte rendu partagé tardivement, quand il l'est. Les conséquences sont connues : décisions oubliées, actions non attribuées, désaccords sur ce qui a été dit, et une personne qui ne participe pas vraiment parce qu'elle écrit.

Dans le contexte des PME camerounaises, trois facteurs aggravent le problème et rendent les outils occidentaux inadaptés : la connexion Internet instable pendant les réunions (les bots de réunion cloud décrochent), une tarification par utilisateur en dollars (16 à 30 \$/utilisateur/mois) hors de portée d'une PME de 5 personnes, et l'absence de paiement par Mobile Money.

1.2 La solution NovaBrief

NovaBrief supprime la prise de notes. L'utilisateur clique sur « Démarrer » au début de sa réunion, participe normalement, clique sur « Terminer ». Quelques minutes plus tard, un compte rendu structuré est disponible : titre, participants identifiés, résumé, décisions, tâches avec responsable et échéance, transcription complète navigable. Rien n'est demandé à l'utilisateur que le système puisse déduire de la conversation.

1.3 Historique de la réflexion et corrections apportées

Ce cahier des charges consolide deux documents produits avec les agents : le cahier des charges v1.0 (cadrage produit, périmètre MVP, architecture fonctionnelle) et la Master Specification (spécification technique détaillée, code de référence). Les corrections suivantes ont été appliquées :

| **Point**                            | **Version précédente**                                                                   | **Correction retenue**                                                                                                                                                                        |
|--------------------------------------|------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Grille tarifaire                     | Deux grilles contradictoires (A : 5 000 / 10 000 / 25 000 ; B : 5 000 / 12 500 / 29 000) | Grille A validée par Novafrik, quotas 10 / 25 / 75 h                                                                                                                                          |
| Taux USD → FCFA                      | 600 FCFA/USD implicite                                                                   | 565 FCFA/USD (cours du 04/09/2026 : 564,75)                                                                                                                                                   |
| Heures traitées à 100 orgs           | 1 225 h (incohérent avec le mix affiché)                                                 | 1 815 h (mix 50/40/10, usage 70 %, packs et comptes gratuits inclus)                                                                                                                          |
| Contrainte « coût technique ≤ 12 % » | Affichée comme respectée                                                                 | Non atteignable avec la grille A en phase API (30 à 36 % du CA HT) ; atteignable seulement en auto-hébergement à grand volume. La contrainte est reformulée en objectif de marge brute ≥ 60 % |
| TVA                                  | Ignorée                                                                                  | 19,25 % appliquée dès le régime du réel (CA \> 50 M FCFA/an) ; prix affichés TTC                                                                                                              |
| Support & opérations                 | 0 FCFA à 100 orgs « grâce à l'IA »                                                       | Comptabilité, outils, signature de code et marketing comptés dès le premier mois ; support salarié à partir de 300 clients                                                                    |
| Renouvellement Mobile Money          | Non modélisé                                                                             | Churn de 4 %/mois incluant le churn involontaire ; relances J-3 / J-1 et période de grâce spécifiées                                                                                          |
| Signature de code Windows            | Absente                                                                                  | Ajoutée (Azure Trusted Signing ≈ 10 \$/mois) : sans elle, SmartScreen bloque l'installateur                                                                                                   |

2\. Vision, proposition de valeur et principes produit

2.1 Vision

Permettre à une équipe de participer pleinement à ses réunions sans prendre de notes, tout en disposant automatiquement d'un compte rendu fidèle et actionnable. À moyen terme, NovaBrief devient la mémoire de l'entreprise : on lui demande « qu'avons-nous décidé sur le budget marketing en juin ? » et il retrouve la réunion, le passage audio et la décision.

2.2 Proposition de valeur

« Vous participez à la réunion. NovaBrief s'occupe du reste. » La valeur n'est pas l'enregistrement (banalisé) mais la transformation de la conversation en connaissance exploitable : des décisions et des tâches fiables, reliées à leur source dans l'audio, sans que personne n'ait rien saisi.

2.3 Les quatre principes produit non négociables

1.  **Déduire plutôt que demander.** Aucune saisie préalable (thème, participants, ordre du jour). Le titre, la date, les participants et la langue sont déduits de la conversation.

2.  **Ne jamais inventer.** Une décision n'est extraite que si elle a été validée dans l'échange ; une tâche n'a un responsable que s'il a été nommé. Une phrase vague (« il faudrait voir pour le plan éditorial ») ne produit rien. La fidélité prime sur la quantité.

3.  **Indépendant de l'outil de réunion.** Pas de bot invité dans Teams ou Meet : NovaBrief capte l'audio du PC (micro + haut-parleurs). Il fonctionne donc avec Teams, Meet, Zoom, WhatsApp Desktop, un softphone ou une réunion physique.

4.  **Offline-first.** L'enregistrement ne dépend jamais du réseau : il est stocké et chiffré localement, puis envoyé quand la connexion est disponible. Une coupure Internet ne détruit jamais une réunion.

3\. Marché et cibles

3.1 Taille du marché adressable

Le Cameroun compte 472 208 PME actives en 2025 (+6,5 % sur un an), dont 78,8 % dans les services ; 57 % des unités formelles sont concentrées à Douala et Yaoundé. Seules 13 % des unités de production nationales sont formelles, ce qui délimite le cœur de cible : les PME formelles de services qui tiennent des réunions structurées.

| **Niveau**  | **Définition**                                                                                                                | **Estimation Cameroun**   | **Hypothèse de pénétration** |
|-------------|-------------------------------------------------------------------------------------------------------------------------------|---------------------------|------------------------------|
| TAM         | PME formelles de services (78,8 % de 472 208)                                                                                 | ≈ 370 000                 | —                            |
| SAM         | PME de 3 à 50 personnes équipées d'ordinateurs, tenant des réunions régulières, à Douala / Yaoundé / Bafoussam (≈ 8 % du TAM) | ≈ 30 000                  | —                            |
| SOM à 3 ans | Organisations payantes atteignables avec un canal partenaires + contenu                                                       | ≈ 950 (scénario réaliste) | ≈ 3 % du SAM                 |

*Sources : News du Camer (25/06/2026) pour le tissu des PME 2025 ; Investir au Cameroun (RGE2) pour la concentration géographique. Le ratio SAM/TAM est une hypothèse Novafrik à confirmer par une enquête terrain sur 50 PME pendant la phase 0.*

3.2 Segments et personas

| **Persona**                                | **Contexte**                                                | **Douleur principale**                                               | **Plan naturel** |
|--------------------------------------------|-------------------------------------------------------------|----------------------------------------------------------------------|------------------|
| Dirigeant de PME de services (5-15 pers.)  | Cabinet conseil, agence, bureau d'études, cabinet comptable | Décisions prises en réunion non suivies ; pas le temps de rédiger    | Starter → Team   |
| Chef de projet en agence digitale          | Réunions clients hebdomadaires sur Meet/Teams               | Comptes rendus clients chronophages, litiges sur ce qui a été validé | Team             |
| Responsable de programme ONG / association | Réunions de coordination, bailleurs, rapports d'activité    | Traçabilité exigée par les bailleurs, équipes dispersées             | Team → Business  |
| Direction de PME structurée (20-50 pers.)  | Comité de direction, réunions de département                | Mémoire d'entreprise, suivi des décisions, confidentialité           | Business         |

3.3 Séquence géographique

Le lancement se fait au Cameroun uniquement pendant 12 mois : un seul pays, une seule monnaie (XAF), un seul PSP (Flutterwave), un réseau de partenaires physique à Douala et Yaoundé. L'expansion vers la zone UEMOA (Côte d'Ivoire, Sénégal) réutilise la même base de code avec un changement de PSP et de prix ; l'Europe (Stripe, RGPD, prix en euros) n'est envisagée qu'après validation du churn et du CAC sur le marché initial.

|                                                                                                                                                                                                                                                                                                                                                                                                                                         |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **POINT D'ATTENTION —** La discussion précédente proposait d'adresser simultanément Cameroun, Afrique et Europe. C'est une dispersion : trois régimes de paiement, deux régimes de conformité et deux grilles de prix à maintenir avant même d'avoir prouvé la rétention sur un marché. L'architecture est prête pour le multi-pays (BillingProvider, prix par marché), mais l'exécution commerciale reste mono-pays la première année. |

4\. Positionnement et concurrence

4.1 Paysage concurrentiel

| **Produit**  | **Plan**       | **Prix / utilisateur / mois**                               | **Mode de capture**              | **Limites pour une PME africaine**                                        |
|--------------|----------------|-------------------------------------------------------------|----------------------------------|---------------------------------------------------------------------------|
| Otter.ai     | Pro / Business | 16,99 \$ / 30 \$                                            | Bot calendrier + app             | Prix par utilisateur en USD, bot intrusif, anglais-centré, carte bancaire |
| Fireflies.ai | Pro / Business | 18 \$ / 29 \$                                               | Bot invité dans la réunion       | Bot visible par les clients, dépend du réseau, US-centric                 |
| Fathom       | Premium / Team | 19 \$ / 29 \$                                               | Bot + app desktop                | Gratuit limité à 5 résumés IA/mois, orienté ventes                        |
| Granola      | Business       | 14 \$                                                       | App desktop (audio système)      | Prise de notes augmentée, pas d'extraction de tâches fiable, anglais      |
| NovaBrief    | Team           | ≈ 18 \$ par organisation (10 000 FCFA), pas par utilisateur | App desktop Windows, audio local | Windows uniquement au lancement                                           |

*Prix publics relevés le 20/03/2026 (granola.ai). NovaBrief se compare à une organisation de 5 utilisateurs : 10 000 FCFA/mois contre 85 à 150 \$/mois chez les concurrents.*

4.2 Positionnement

NovaBrief ne se vend pas comme « un outil qui enregistre vos réunions avec l'IA » : ce marché est saturé et la transcription est une commodité. Il se vend comme **le seul outil qui transforme vos réunions en décisions et en actions fiables, sans bot, sans notes, et qui fonctionne au Cameroun** : prix par organisation en FCFA, Mobile Money, hors connexion, français et anglais avec accents locaux.

Les trois axes de différenciation à défendre dans le produit et dans la communication sont : la **fiabilité** (on n'invente rien, chaque élément renvoie à sa source audio), l'**invisibilité** (aucun bot dans la réunion, l'outil est discret) et l'**adaptation locale** (paiement, prix, langue, réseau).

5\. Modèle économique

5.1 L'unité économique : l'heure de réunion traitée

Tout le modèle repose sur une unité : l'heure de réunion traitée. Chaque heure déclenche une transcription, une diarisation, une analyse LLM et un stockage temporaire. Les tarifs ci-dessous ont été vérifiés en septembre 2026 (sources détaillées dans la feuille Sources du modèle Excel).

| **Poste**                | **Fournisseur**                                | **Tarif vérifié**                                                              | **USD / h** | **FCFA / h** |
|--------------------------|------------------------------------------------|--------------------------------------------------------------------------------|-------------|--------------|
| Capture et mixage audio  | Local (Rust, PC du client)                     | —                                                                              | 0           | 0            |
| Transcription            | AssemblyAI Universal-3.5 Pro (async)           | 0,21 \$/h, facturé à la seconde                                                | 0,2100      | 118,7        |
| Diarisation              | AssemblyAI (add-on)                            | +0,02 \$/h                                                                     | 0,0200      | 11,3         |
| Analyse LLM              | OpenAI GPT-5 mini                              | 0,25 \$ / 1M tokens in, 2 \$ / 1M out ; 16 000 tokens in, 2 000 out, 1,5 passe | 0,0120      | 6,8          |
| Stockage audio 30 jours  | Cloudflare R2                                  | 0,015 \$/Go-mois, egress gratuit ; 14,4 Mo/h (Opus 32 kbps)                    | 0,0002      | 0,1          |
| Stockage texte           | PostgreSQL (infra fixe)                        | négligeable                                                                    | 0,0002      | 0,1          |
| Sous-total direct        |                                                |                                                                                | 0,2424      | 137,0        |
| Sécurité technique 10 %  | Relances, fallback Deepgram, réunions échouées |                                                                                | 0,0242      | 13,7         |
| **Coût variable budget** |                                                |                                                                                | **0,2667**  | **150,7**    |

[figure]

*Figure 1 — Décomposition du coût variable par heure traitée. La transcription représente 79 % du coût ; c'est le seul poste qui mérite une stratégie d'optimisation.*

Deux enseignements. D'abord, l'IA d'analyse est marginale (4,5 % du coût) : il n'y a aucune raison d'utiliser un modèle plus cher que GPT-5 mini pour le compte rendu standard, et GPT-5 nano (0,05 \$ / 0,40 \$) peut être testé pour le titre et les participants. Ensuite, la transcription est le poste à surveiller : le passage à Whisper auto-hébergé divise ce poste par quatre, mais seulement au-delà de quelques milliers d'heures par mois (section 5.8).

5.2 Grille tarifaire A et règles commerciales

Les prix sont affichés TTC en FCFA, par organisation et par mois, avec un quota d'heures de réunion traitées. La facturation par organisation (et non par utilisateur) est un choix délibéré : elle simplifie l'achat pour une PME, évite le partage de comptes et fait de l'heure traitée la seule variable de coût.

| **Plan** | **Prix TTC / mois** | **Quota** | **Prix / heure** | **Cible**                    | **Inclus**                                                                          |
|----------|---------------------|-----------|------------------|------------------------------|-------------------------------------------------------------------------------------|
| Free     | 0 FCFA              | 2 h       | —                | Découverte                   | Réunions ≤ 60 min, historique 30 jours, pas de conservation audio, export filigrané |
| Starter  | 5 000 FCFA          | 10 h      | 500 FCFA         | TPE, indépendants, 1-5 pers. | Comptes rendus, transcription, 3 utilisateurs, audio 30 jours                       |
| Team     | 10 000 FCFA         | 25 h      | 400 FCFA         | PME 5-20 pers.               | Starter + utilisateurs illimités, recherche plein texte, validation collaborative   |
| Business | 25 000 FCFA         | 75 h      | 333 FCFA         | PME 20-50 pers., ONG         | Team + audio 90 jours, journal d'audit, export API, support prioritaire             |
| Pack 5 h | 2 500 FCFA          | 5 h       | 500 FCFA         | Dépassement                  | Heures additionnelles valables jusqu'à la fin du cycle                              |

**Règles intégrées au produit :**

- **Pas de report (rollover).** Les heures non consommées expirent à la fin du cycle de 30 jours. C'est la principale source de marge (breakage) : à 70 % d'utilisation, 30 % des heures vendues ne coûtent rien.

- **Dépassement bloquant mais non destructif.** À 100 % du quota, l'enregistrement reste possible ; le traitement est mis en attente jusqu'à l'achat d'un pack ou le renouvellement. L'audio n'est jamais perdu.

- **Cycle de 30 jours glissants** à partir du paiement, indépendant du mois calendaire (compatibilité Mobile Money).

- **Prix internationaux indicatifs** pour les marchés hors CEMAC (phase 3) : 9 € / 19 € / 45 € HT, soit des marges brutes de 71 à 78 % après frais Stripe.

|                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **POINT D'ATTENTION —** Le plan Starter à 5 000 FCFA pour 10 h est le plus exposé : à pleine utilisation, son coût technique atteint 30 % du prix (61 % de marge brute HT au pire cas). Il reste rentable, mais il faut le traiter comme une porte d'entrée et pousser vers Team dès qu'une organisation dépasse 7 h/mois deux mois de suite. Le quota Starter est un paramètre du modèle Excel : le ramener à 8 h porterait la marge au pire cas à 69 %. |

5.3 Marges par plan

| **Indicateur (FCFA)**                          | **Starter** | **Team** | **Business** | **Pack 5 h** |
|------------------------------------------------|-------------|----------|--------------|--------------|
| Prix TTC                                       | 5 000       | 10 000   | 25 000       | 2 500        |
| Prix HT (TVA 19,25 % appliquée)                | 4 193       | 8 386    | 20 964       | 2 096        |
| Coût technique à 100 % d'usage                 | 1 507       | 3 766    | 11 299       | 753          |
| Coût technique à 70 % d'usage                  | 1 055       | 2 637    | 7 910        | 527          |
| Frais PSP (2 % MoMo / 4,8 % cartes, mix 85/15) | 121         | 242      | 605          | 61           |
| Marge brute à 70 % (prix HT)                   | 3 017       | 5 507    | 12 450       | 1 509        |
| Marge brute % (prix HT, 70 %)                  | 72,0 %      | 65,7 %   | 59,4 %       | 72,0 %       |
| Marge brute % avant seuil TVA (prix TTC)       | 76,5 %      | 71,2 %   | 65,9 %       | 76,5 %       |
| Marge brute au pire cas (100 %, HT)            | 61,2 %      | 52,2 %   | 43,2 %       | 61,2 %       |

[figure]

*Figure 2 — La marge en pourcentage décroît avec la taille du plan (prix dégressif par heure) : Business rapporte le plus en valeur absolue mais le moins en pourcentage. L'utilisation réelle du quota est la variable à mesurer dès le premier mois.*

|                                                                                                                                                                                                                                                                                                                           |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **RECOMMANDATION —** Mesurer dès la bêta le taux d'utilisation réel par plan. Si les clients Business consomment plus de 85 % de leur quota, relever le prix Business à 29 000 FCFA (grille B) ou réduire le quota à 60 h : c'est un simple paramètre, mais il doit être décidé avant l'ouverture commerciale, pas après. |

5.4 Fiscalité et paiement : les deux frottements que le modèle ne doit pas ignorer

TVA

Tant que le chiffre d'affaires annuel de l'activité reste inférieur à 50 M FCFA, Novafrik n'est pas assujettie à la TVA pour NovaBrief. Au-delà (régime du réel), la TVA de 19,25 % s'applique : un prix affiché de 5 000 FCFA TTC ne rapporte plus que 4 193 FCFA HT, soit −16 %. Le modèle applique ce basculement automatiquement dès que le MRR annualisé dépasse le seuil (vers le 24e mois dans le scénario réaliste). Les PME clientes étant pour la plupart non assujetties (IGS), la TVA n'est pas récupérable pour elles : afficher des prix TTC simples est la bonne pratique. L'impôt sur les sociétés est retenu à 33 % (30 % + CAC), avec le minimum de perception de 2,2 % du CA ; un taux réduit à 27,5 % pour les sociétés dont le CA est inférieur à 3 Mds FCFA est à confirmer avec l'expert-comptable.

Mobile Money

Flutterwave facture au Cameroun 2 % par transaction Mobile Money (MTN MoMo, Orange Money), 4,8 % sur cartes, et 1 500 FCFA par virement vers la banque, avec règlement J+1. Le frottement n'est pas le coût mais l'absence de prélèvement automatique : chaque renouvellement exige une validation USSD par le client. Sans dispositif de relance, le churn involontaire peut dépasser le churn volontaire. Le produit intègre donc : rappel J-3 (email + notification desktop), rappel J-1, lien de paiement en un clic, période de grâce de 3 jours pendant laquelle le traitement continue, puis mise en attente des traitements (jamais de perte de données). Le churn de 4 % retenu dans le modèle intègre ce churn involontaire résiduel.

5.5 P&L en régime de croisière : 100, 1 000 et 10 000 organisations

Le tableau ci-dessous présente un mois type à trois échelles, avec des hypothèses cohérentes (mix de plans, usage 70 %, 2 comptes gratuits actifs par client payant, remplacement du churn par de l'acquisition, support salarié au-delà de 300 clients). La colonne 10 000 est illustrative : elle suppose la bascule vers Whisper auto-hébergé et l'ouverture d'autres pays.

| **FCFA / mois**                               | **100 orgs**   | **1 000 orgs**     | **10 000 orgs**       |
|-----------------------------------------------|----------------|--------------------|-----------------------|
| Mix Starter / Team / Business                 | 50 / 40 / 10 % | 45 / 40 / 15 %     | 35 / 45 / 20 %        |
| Heures traitées (payantes + packs + gratuits) | 1 815          | 20 425             | 232 250               |
| CA TTC encaissé (MRR)                         | 920 000        | 10 200 000         | 114 500 000           |
| TVA collectée                                 | 0 (sous seuil) | 1 646 541          | 18 483 229            |
| CA HT                                         | 920 000        | 8 553 459          | 96 016 771            |
| Coût variable API et stockage                 | 273 445        | 3 077 198          | 1 791 437 (Whisper)   |
| Frais PSP                                     | 28 264         | 252 840            | 2 776 900             |
| Marge brute                                   | 618 291 (67 %) | 5 223 421 (61 %)   | 91 448 435 (95 %)     |
| Infrastructure (Hetzner) + GPU                | 20 991         | 62 316             | 5 667 469             |
| Outils, signature de code, email              | 20 644         | 31 944             | 31 944                |
| Comptabilité                                  | 50 000         | 50 000             | 50 000                |
| Support client                                | 0              | 300 000 (2 agents) | 3 000 000 (20 agents) |
| Marketing (remplacement du churn + fixe)      | 132 000        | 420 000            | 3 300 000             |
| Rémunération fondateurs                       | 0 (paramètre)  | 0 (paramètre)      | 0 (paramètre)         |
| EBITDA                                        | 394 656 (43 %) | 4 359 161 (51 %)   | 79 399 022 (83 %)     |
| IS estimé (33 %, min. 2,2 % du CA)            | 130 236        | 1 438 523          | 26 201 677            |
| Résultat net mensuel                          | 264 420        | 2 920 638          | 53 197 345            |
| Résultat net annualisé                        | 3,2 M          | 35,0 M             | 638 M                 |
| Coût technique / CA HT                        | 29,7 %         | 36,0 %             | 1,9 %                 |
| LTV / CAC (churn 4 %, CAC 8 000)              | 19,3 x         | 16,3 x             | 28,6 x                |

À 1 000 organisations, NovaBrief génère environ 2,9 M FCFA de résultat net mensuel (≈ 4 450 €) avant rémunération des fondateurs, avec un MRR de 10,2 M FCFA. C'est un revenu solide pour une équipe de deux fondateurs au Cameroun, mais ce n'est pas encore une entreprise à l'échelle : la marche suivante (10 000 organisations, 175 000 \$ de MRR) exige plusieurs pays, une équipe support et une bascule technologique sur la transcription.

5.6 Projection mensuelle sur 36 mois

Le scénario réaliste part d'un développement par agent IA d'octobre à décembre 2026 (POC puis MVP), d'une bêta privée en janvier 2027 auprès de 5 PME de Douala et Yaoundé, et de premiers clients payants en février 2027 (M5). La courbe d'acquisition monte de 5 nouveaux clients par mois au lancement à 80 par mois en troisième année, ce qui suppose un canal partenaires actif (cabinets comptables, consultants, incubateurs) et une extension à un second pays en année 2. Le churn est fixé à 4 % par mois, le CAC à 8 000 FCFA.

| **Indicateur**            | **M6 (mars-27)** | **M12 (sept-27)** | **M18 (mars-28)** | **M24 (sept-28)** | **M30 (mars-29)** | **M36 (sept-29)** |
|---------------------------|------------------|-------------------|-------------------|-------------------|-------------------|-------------------|
| Nouveaux clients / mois   | 8                | 22                | 38                | 52                | 68                | 80                |
| Clients payants actifs    | 13               | 99                | 248               | 447               | 685               | 950               |
| Comptes gratuits actifs   | 26               | 198               | 496               | 894               | 1 370             | 1 900             |
| Heures traitées / mois    | 266              | 2 022             | 5 065             | 9 130             | 13 991            | 19 404            |
| MRR TTC (FCFA)            | 132 600          | 1 009 800         | 2 529 600         | 4 559 400         | 6 987 000         | 9 690 000         |
| CA HT (FCFA)              | 132 600          | 1 009 800         | 2 529 600         | 3 823 396         | 5 859 119         | 8 125 786         |
| Marge brute %             | 63 %             | 67 %              | 67 %              | 61 %              | 61 %              | 61 %              |
| EBITDA (FCFA)             | −172 247         | 307 085           | 1 162 278         | 1 521 291         | 2 487 893         | 3 777 690         |
| Résultat net (FCFA)       | −175 165         | 205 747           | 778 727           | 1 019 265         | 1 666 889         | 2 531 052         |
| Trésorerie cumulée (FCFA) | −1 246 082       | −1 060 505        | 2 015 791         | 8 048 278         | 16 223 741        | 29 196 568        |

[figure]

*Figure 3 — Clients payants actifs (scénario réaliste). 1 398 clients acquis sur 36 mois, 950 actifs à M36 après churn.*

[figure]

*Figure 4 — MRR et EBITDA mensuels. La rupture de pente de l'EBITDA à M24 correspond à l'assujettissement à la TVA.*

[figure]

*Figure 5 — Trésorerie cumulée. Le point bas (−1,46 M FCFA, ≈ 2 220 €) est atteint à M9 ; le projet s'autofinance ensuite.*

**Point mort et besoin de financement.** Le premier mois d'EBITDA positif est M9 (juin 2027) avec 47 clients payants. Le besoin de trésorerie maximal est de 1,46 M FCFA, réparti entre le budget POC (200 000 FCFA), le juridique (400 000 FCFA), l'infrastructure et le marketing de lancement. Ce chiffre exclut la rémunération des fondateurs : avec 600 000 FCFA de salaires mensuels, le point mort recule à M15 (décembre 2027) et le besoin de trésorerie passe à 8,4 M FCFA.

5.7 Scénarios et sensibilité

| **Scénario**                 | **Clients M12** | **Clients M24** | **Clients M36** | **MRR M36** | **EBITDA M36** | **Point mort** | **Besoin tréso.** |
|------------------------------|-----------------|-----------------|-----------------|-------------|----------------|----------------|-------------------|
| Réaliste (base)              | 99              | 447             | 950             | 9 690 000   | 3 777 690      | M9 (juin-27)   | 1 456 527         |
| Prudent (acquisition ×0,5)   | 51              | 226             | 478             | 4 875 600   | 1 779 403      | M11 (août-27)  | 1 680 497         |
| Ambitieux (acquisition ×1,5) | 150             | 672             | 1 426           | 14 545 200  | 5 564 030      | M8 (mai-27)    | 1 329 453         |
| Churn 8 %                    | 89              | 357             | 692             | 7 058 400   | 2 428 499      | M10 (juil-27)  | 1 483 134         |
| Fondateurs 600 000 FCFA/mois | 99              | 447             | 950             | 9 690 000   | 3 177 690      | M15 (déc-27)   | 8 430 989         |
| Usage 100 %                  | 99              | 447             | 950             | 9 690 000   | 2 439 181      | M10 (juil-27)  | 1 596 188         |
| CAC 16 000 + churn 6 %       | 94              | 399             | 802             | 8 180 400   | 2 363 736      | M11 (août-27)  | 1 948 533         |

[figure]

*Figure 6 — Clients payants actifs à M36 selon le scénario. Même le scénario prudent (acquisition divisée par deux) reste rentable dès M11.*

La sensibilité montre que le modèle est robuste aux hypothèses techniques et fragile aux hypothèses commerciales. À 1 000 organisations, faire varier le prix de la transcription de 0,10 à 0,36 \$/h ne déplace l'EBITDA mensuel que de 5,8 à 2,5 M FCFA (à 70 % d'usage) ; en revanche, un churn de 8 % au lieu de 4 % retire 27 % des clients actifs à M36, et une utilisation de 100 % du quota au lieu de 70 % ampute l'EBITDA de M36 de 35 %. Le ratio LTV/CAC reste supérieur à 4 même avec un CAC doublé (16 000 FCFA) et un churn de 8 %.

5.8 Stratégie de réduction des coûts : quand basculer vers Whisper auto-hébergé

Un serveur GPU Hetzner GEX44 (RTX 4000 SFF Ada 20 Go) coûte 184 €/mois plus 159 € d'installation. Avec Whisper large-v3 à 20 fois le temps réel, 60 % d'utilisation exploitable et 30 % de surcoût pour la diarisation, un GPU traite environ 6 650 heures par mois. Deux machines sont nécessaires en production pour la redondance.

| **Heures / mois** | **Coût API (USD)** | **GPU nécessaires** | **Coût auto-hébergé (USD)** | **Auto-hébergé USD / h** | **Économie**   | **Verdict**                          |
|-------------------|--------------------|---------------------|-----------------------------|--------------------------|----------------|--------------------------------------|
| 1 000             | 230                | 2                   | 543                         | 0,543                    | −313           | Rester sur l'API                     |
| 2 500             | 575                | 2                   | 543                         | 0,217                    | +32            | Économie faible vs risque qualité    |
| 5 000             | 1 150              | 2                   | 543                         | 0,109                    | +607 (53 %)    | Négocier volume, préparer la bascule |
| 10 000            | 2 300              | 2                   | 543                         | 0,054                    | +1 757 (76 %)  | Basculer                             |
| 50 000            | 11 500             | 8                   | 2 173                       | 0,043                    | +9 327 (81 %)  | Basculer                             |
| 150 000           | 34 500             | 23                  | 6 248                       | 0,042                    | +28 252 (82 %) | Basculer                             |

[figure]

*Figure 7 — Seuil de rentabilité brut de l'auto-hébergement ≈ 2 360 h/mois (≈ 115 organisations Team). La bascule est recommandée à partir de 5 000 h/mois seulement, après benchmark qualité.*

La feuille de route en trois phases est la suivante. **Phase 1 (0 à 2 500 h/mois)** : API AssemblyAI, fallback Deepgram, aucune infrastructure GPU ; l'objectif est la qualité et la vitesse de mise sur le marché. **Phase 2 (2 500 à 10 000 h/mois)** : négociation d'un tarif volume avec AssemblyAI et Deepgram, benchmark Whisper large-v3 sur 200 réunions réelles en français et en anglais avec accents camerounais (WER, qualité de la diarisation), décision documentée. **Phase 3 (\> 10 000 h/mois)** : cluster GPU auto-hébergé derrière la même interface TranscriptionProvider, l'API restant le fallback.

5.9 Le tableau de bord d'unit economics

Le tableau de bord interne est un composant du MVP, pas un « nice to have » : chaque réunion écrit dans un registre de consommation (usage_ledger) les secondes traitées et le coût unitaire réel facturé par les fournisseurs. Les indicateurs suivants sont calculés chaque semaine et affichés dans l'administration Novafrik :

| **Indicateur**                            | **Formule**                                                                       | **Seuil d'alerte**   |
|-------------------------------------------|-----------------------------------------------------------------------------------|----------------------|
| MRR, ARPU HT                              | Somme des abonnements actifs ; CA HT / clients actifs                             | ARPU \< 8 000 FCFA   |
| Coût variable par heure                   | Coûts fournisseurs du mois / heures traitées                                      | \> 180 FCFA          |
| Marge brute %                             | (CA HT − coûts variables − PSP) / CA HT                                           | \< 55 %              |
| Taux d'utilisation par plan               | Heures traitées / heures de quota                                                 | \> 85 % sur Business |
| Churn mensuel (volontaire / involontaire) | Clients perdus / clients début de mois, ventilé par cause                         | \> 6 %               |
| Taux de renouvellement MoMo à J+3         | Renouvellements validés / échéances                                               | \< 80 %              |
| CAC et payback                            | Dépenses marketing / nouveaux clients ; CAC / marge brute par client              | Payback \> 4 mois    |
| LTV / CAC                                 | (Marge brute par client / churn) / CAC                                            | \< 3                 |
| NovaBrief Score                           | (tâches validées / tâches proposées) × (décisions validées / décisions proposées) | \< 85 %              |
| Coût des comptes gratuits                 | Heures Free × coût/h / marge brute                                                | \> 8 %               |

5.10 Points de vigilance économiques

- **Les comptes gratuits coûtent de l'argent réel.** À 1 000 clients payants, 2 000 comptes Free consomment 2 000 h/mois, soit 300 000 FCFA (3,5 % du CA HT). Le quota Free de 2 h est acceptable ; il doit être réduit à 1 h si la conversion Free → payant tombe sous 10 %.

- **Le CAC de 8 000 FCFA est optimiste** pour une acquisition qui ne serait pas majoritairement organique. Il correspond à un canal partenaires (commission 10 % la première année) et à du contenu ; des campagnes payantes doubleraient ce chiffre. Le ratio LTV/CAC laisse une marge confortable, mais il faut mesurer le CAC réel dès le troisième mois.

- **La TVA change la marge à M24.** Anticiper la bascule au régime du réel avec l'expert-comptable, et vérifier la possibilité de facturer HT aux clients assujettis (grands comptes, ONG) qui récupèrent la TVA.

- **Le mix de plans est une hypothèse.** Si 70 % des clients restent sur Starter, l'ARPU tombe à ~6 500 FCFA et l'EBITDA à 1 000 orgs recule d'un tiers. Le produit doit rendre l'upsell naturel (alerte à 70 % du quota, essai gratuit de la recherche plein texte).

- **Aucune rémunération des fondateurs n'est incluse.** Le modèle montre l'économie du produit ; un P&L d'entreprise doit ajouter les salaires, ce qui repousse le point mort de six mois.

6\. Stratégie de mise sur le marché

6.1 Canaux d'acquisition (par ordre de priorité)

1.  **Partenaires prescripteurs** : cabinets d'expertise comptable, consultants en organisation, incubateurs et espaces de coworking de Douala et Yaoundé. Ils voient des dizaines de PME et sont crédibles ; commission de 10 % du MRR pendant 12 mois, tableau de bord partenaire dans l'administration.

2.  **Bêta privée puis témoignages** : 5 PME en bêta (janvier 2027) transformées en études de cas publiées (LinkedIn, YouTube) avec des comptes rendus réels anonymisés.

3.  **Contenu et démonstration** : vidéo de 90 secondes « réunion → compte rendu », page de démonstration avec un compte rendu interactif, articles sur la conduite de réunion en PME.

4.  **Offre de lancement** : 3 mois de Team au prix de Starter pour les 100 premières organisations, en échange d'un retour structuré (interview de 20 minutes).

5.  **Publicité payante** : uniquement après mesure du CAC organique, et uniquement LinkedIn / Meta ciblés sur les dirigeants de PME de services.

6.2 Parcours d'activation

L'activation est mesurée par un seul événement : « première réunion traitée avec succès ». Tout le parcours d'inscription est conçu pour y amener l'utilisateur en moins de 15 minutes : inscription par email ou numéro de téléphone, téléchargement de l'application Windows (\< 15 Mo), test audio guidé de 30 secondes qui vérifie que le micro et l'audio système sont captés, puis une réunion d'essai. Un compte qui n'a pas traité de réunion 48 h après l'inscription reçoit un email d'aide et, pour les inscrits partenaires, un appel.

6.3 Indicateurs de pilotage commercial

| **Phase**          | **Indicateur**                                     | **Objectif**        |
|--------------------|----------------------------------------------------|---------------------|
| Bêta (M4)          | Réunions traitées par organisation / semaine       | ≥ 2                 |
| Bêta (M4)          | NovaBrief Score sur 50 réunions                    | ≥ 85 %              |
| Lancement (M5-M12) | Activation (première réunion traitée sous 7 jours) | ≥ 60 % des inscrits |
| Lancement (M5-M12) | Conversion Free → payant à 30 jours                | ≥ 15 %              |
| Lancement (M5-M12) | Churn mensuel                                      | ≤ 5 %               |
| Expansion (M13+)   | Part des clients Team + Business                   | ≥ 55 %              |
| Expansion (M13+)   | CAC réel                                           | ≤ 10 000 FCFA       |

**PARTIE II — SPÉCIFICATIONS FONCTIONNELLES**

*Cette partie décrit ce que le produit fait, pour qui, et comment il se comporte dans chaque situation, y compris les cas d'erreur. Chaque exigence est numérotée (EF-xx) et priorisée : M = Must (MVP), S = Should (V1), C = Could (V1.x). L'agent IA de développement doit pouvoir dériver ses tickets directement de ces exigences.*

7\. Périmètre du MVP et hors périmètre

7.1 Définition du MVP

Le MVP est la plus petite version qui permette à une PME camerounaise d'installer NovaBrief, d'enregistrer une réunion Teams ou Meet, d'obtenir un compte rendu fiable, de le corriger, et de payer par Mobile Money. Tout ce qui n'est pas nécessaire à cette boucle est exclu, même si c'est facile à développer.

| **Domaine**             | **Must (MVP — V1.0)**                                                                                                                                            | **Should (V1.1)**                                                    | **Could (V1.x)**                                               |
|-------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------|----------------------------------------------------------------|
| Compte & organisation   | Inscription, connexion, mot de passe oublié, organisation, invitation de membres, rôles Owner / Admin / Member                                                   | Connexion Google                                                     | SSO entreprise                                                 |
| Desktop Windows         | Installation signée, connexion, tray, Démarrer / Pause / Terminer, vu-mètres, stockage local chiffré, upload reprise, mise à jour automatique, raccourcis        | Détection automatique de réunion (proposition d'enregistrer)         | Client macOS                                                   |
| Traitement              | Transcription FR/EN avec détection de langue, diarisation, titre, participants, résumé, décisions, tâches, échéances, timestamps source                          | Résumé par section (ordre du jour déduit)                            | Résumé personnalisé par rôle                                   |
| Web                     | Tableau de bord, liste des réunions, lecteur (compte rendu + transcription synchronisée + audio), validation / rejet des éléments, édition du titre, suppression | Recherche plein texte, partage d'un compte rendu par lien            | Recherche sémantique multi-réunions (« mémoire d'entreprise ») |
| Export & notifications  | Export PDF, notification desktop et in-app « compte rendu prêt », email de fin de traitement                                                                     | Export Word / Markdown, envoi automatique par email aux participants | Intégrations Slack / Teams / Notion / Trello                   |
| Facturation             | Plans Free / Starter / Team / Business, quotas, packs, Mobile Money (Flutterwave), relances de renouvellement, factures PDF                                      | Stripe (Europe), codes partenaires                                   | Facturation annuelle, prélèvement automatique carte            |
| Administration Novafrik | Back-office : organisations, consommation, coûts, relance manuelle d'un traitement, tableau de bord unit economics                                               | Gestion des partenaires et commissions                               | Feature flags par organisation                                 |

7.2 Explicitement hors périmètre

Sont exclus jusqu'à validation commerciale du MVP : gestion de projet et système de tâches complet, calendrier, CRM, intégrations natives Teams / Meet / Zoom (bots), transcription en temps réel, application mobile (hormis la consultation web responsive), assistant vocal, chatbot conversationnel sur l'historique, automatisation des échéances, application macOS ou Linux. Ces exclusions sont des décisions, pas des oublis : chacune ajouterait de la surface à maintenir avant que le cœur ne soit prouvé.

8\. Acteurs et rôles

| **Acteur**               | **Description**                                           | **Droits principaux**                                                                                                                                                        |
|--------------------------|-----------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Owner                    | Créateur de l'organisation, responsable de la facturation | Tout ; gestion du plan, du paiement, suppression de l'organisation, export des données                                                                                       |
| Admin                    | Responsable d'équipe désigné par l'Owner                  | Gestion des membres, de la conservation, consultation de toutes les réunions de l'organisation, validation des comptes rendus                                                |
| Member                   | Utilisateur standard                                      | Enregistrer des réunions, consulter les réunions de l'organisation (sauf réunions marquées privées par leur auteur), valider / corriger les éléments de ses propres réunions |
| Invité (lien de partage) | Personne externe recevant un compte rendu (V1.1)          | Lecture seule d'un compte rendu, sans audio ni transcription si l'auteur le décide                                                                                           |
| Opérateur Novafrik       | Équipe Novafrik (support, finance)                        | Back-office : consommation, coûts, relance de traitement, remboursement ; jamais d'accès au contenu des réunions sans consentement explicite du client (accès tracé)         |
| Système (workers)        | Pipeline de traitement                                    | Lecture de l'audio le temps du traitement, écriture des résultats, purge                                                                                                     |

9\. Parcours utilisateurs de référence

9.1 Parcours P1 — Première réunion (activation)

1.  L'utilisateur crée un compte sur app.novabrief.com (email ou numéro de téléphone, mot de passe, nom de l'organisation). Un email de vérification est envoyé ; le compte est utilisable immédiatement, la vérification est requise sous 7 jours.

2.  Il télécharge l'installateur Windows (signé, \< 15 Mo). L'installation ne demande aucun droit administrateur (installation par utilisateur).

3.  Au premier lancement, il se connecte (ou scanne un code affiché sur le web pour lier l'application sans ressaisir de mot de passe). L'application propose un test audio de 30 secondes : elle lit un son de test dans les haut-parleurs, demande à l'utilisateur de dire une phrase, et confirme visuellement que les deux flux sont captés.

4.  Il rejoint sa réunion Teams / Meet / Zoom comme d'habitude et clique sur « Démarrer » (ou Ctrl+Shift+R). L'indicateur rouge et les vu-mètres confirment l'enregistrement.

5.  En fin de réunion, il clique sur « Terminer et analyser ». L'application finalise le fichier, l'envoie, et affiche « Traitement en cours, ~4 minutes ».

6.  Une notification desktop « Votre compte rendu est prêt » ouvre le compte rendu dans le navigateur. L'utilisateur lit le résumé, valide ou rejette les décisions et les tâches, corrige un nom de participant si nécessaire.

9.2 Parcours P2 — Réunion avec coupure réseau

La connexion tombe à la 20e minute d'une réunion de 45 minutes. L'enregistrement continue sans interruption (aucune dépendance réseau). À « Terminer », l'application affiche « Hors ligne : le compte rendu sera envoyé dès que la connexion reviendra » et conserve le fichier chiffré localement. Au retour du réseau (détection automatique, vérification toutes les 30 secondes), l'upload reprend au dernier segment confirmé. Si l'utilisateur ferme l'application, l'upload reprend au prochain démarrage. Aucun clic n'est demandé.

9.3 Parcours P3 — Quota atteint

À 70 % du quota, une notification informe l'Owner (« 17 h 30 sur 25 h utilisées, renouvellement le 14/03 »). À 100 %, l'enregistrement reste possible ; à la fin de la réunion, l'application indique « Quota atteint : le compte rendu sera généré après l'achat d'un pack (2 500 FCFA / 5 h) ou au renouvellement ». Le lien de paiement Mobile Money est affiché dans l'application et envoyé par email. Dès validation du paiement (webhook), le traitement démarre automatiquement.

9.4 Parcours P4 — Renouvellement Mobile Money

J-3 avant l'échéance : email et notification desktop avec un lien de paiement pré-rempli (montant, numéro MoMo enregistré). L'Owner clique, reçoit une demande USSD sur son téléphone, saisit son code PIN. Le webhook Flutterwave confirme le paiement en quelques secondes ; le cycle est renouvelé et une facture PDF est envoyée. J-1 : second rappel si non payé. J+0 à J+3 : période de grâce, service maintenu, bandeau d'avertissement. J+4 : les nouveaux traitements sont mis en attente (les enregistrements restent possibles et conservés), les comptes rendus existants restent consultables. J+30 sans paiement : passage au plan Free avec conservation des données 90 jours, puis suppression après notification.

9.5 Parcours P5 — Correction et apprentissage

Dans le lecteur, chaque décision et chaque tâche porte deux actions : Valider et Rejeter, plus une édition en ligne (texte, responsable choisi parmi les membres, échéance). Chaque action est enregistrée avec l'élément d'origine, la version corrigée et le passage de transcription source. Ces retours alimentent le jeu d'évaluation interne (section 18.6) et le NovaBrief Score affiché dans l'administration ; ils n'entraînent aucun modèle sans décision explicite de Novafrik.

10\. Exigences fonctionnelles détaillées

10.1 Compte et organisation

| **Réf.** | **Exigence**                                                                                                                                                                                                 | **Prio.** | **Critère d'acceptation**                                                                                       |
|----------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|-----------------------------------------------------------------------------------------------------------------|
| EF-01    | Inscription par email ou numéro de téléphone (format E.164), mot de passe ≥ 10 caractères, création d'organisation en une étape                                                                              | M         | Un utilisateur crée un compte et une organisation en \< 2 minutes sans aide                                     |
| EF-02    | Vérification de l'email ; connexion ; déconnexion ; réinitialisation du mot de passe par lien à usage unique valable 30 min                                                                                  | M         | Le lien expiré ou réutilisé est refusé avec un message clair                                                    |
| EF-03    | Invitation de membres par email avec rôle ; acceptation en un clic ; révocation                                                                                                                              | M         | Un membre révoqué perd l'accès en \< 60 s (révocation du refresh token)                                         |
| EF-04    | Profil : nom affiché, langue de l'interface (FR / EN), fuseau horaire (par défaut Africa/Douala)                                                                                                             | M         | Les dates des comptes rendus s'affichent dans le fuseau de l'utilisateur                                        |
| EF-05    | Paramètres d'organisation : nom, identifiant légal (RCCM / NIU, optionnel), langue par défaut des réunions, durée de conservation audio (30 / 90 jours selon plan), lexique métier (noms propres, acronymes) | M         | Le lexique est transmis au moteur de transcription (keyterms) et améliore la reconnaissance des noms            |
| EF-06    | Export complet des données de l'organisation (JSON + PDF + audio non purgé) et suppression définitive sur demande de l'Owner, avec délai de rétractation de 7 jours                                          | M         | Après suppression, aucune donnée de l'organisation ne subsiste hors sauvegardes chiffrées purgées sous 30 jours |

10.2 Application desktop

| **Réf.** | **Exigence**                                                                                                                                                                                  | **Prio.** | **Critère d'acceptation**                                                            |
|----------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|--------------------------------------------------------------------------------------|
| EF-10    | Installateur Windows 10/11 x64 signé (Azure Trusted Signing), installation par utilisateur sans droits administrateur, \< 15 Mo, démarrage automatique optionnel avec Windows                 | M         | Aucun avertissement SmartScreen ; installation en \< 30 s                            |
| EF-11    | Connexion par email / mot de passe ou par code de liaison affiché sur le web (6 caractères, 10 min) ; session persistante par refresh token stocké dans le coffre Windows (DPAPI)             | M         | Une fois lié, l'utilisateur n'a plus jamais à se reconnecter sauf révocation         |
| EF-12    | Icône dans la zone de notification avec menu : Démarrer, Mes réunions, Test audio, Paramètres, Quitter ; état visible (inactif / enregistrement / envoi / traitement)                         | M         | L'état de l'application est identifiable en un coup d'œil sans ouvrir de fenêtre     |
| EF-13    | Widget flottant pendant l'enregistrement : indicateur rouge pulsé, chronomètre, deux vu-mètres (micro / système), boutons Pause et Terminer, toujours au premier plan, déplaçable, réductible | M         | Un utilisateur détecte en \< 5 s que l'audio système n'est pas capté (vu-mètre plat) |
| EF-14    | Raccourcis globaux configurables : Ctrl+Shift+R (démarrer / terminer), Ctrl+Shift+P (pause)                                                                                                   | M         | Fonctionnent quand Teams a le focus                                                  |
| EF-15    | Sélection des périphériques (micro, sortie) avec valeurs par défaut Windows ; détection du changement de périphérique en cours d'enregistrement (casque Bluetooth branché) sans interruption  | M         | Un changement de périphérique produit au plus 500 ms de silence, jamais une coupure  |
| EF-16    | Test audio guidé de 30 s : vérifie la capture des deux flux, le niveau et l'absence d'écho ; recommandations affichées (utiliser un casque, augmenter le volume)                              | M         | Le test échoue explicitement si le loopback ne renvoie rien                          |
| EF-17    | Stockage local des enregistrements chiffrés (AES-256-GCM, clé dérivée de la session) dans %LOCALAPPDATA%\NovaBrief, segments de 5 s, reprise après crash ou coupure d'alimentation            | M         | Un arrêt brutal du PC perd au plus les 5 dernières secondes                          |
| EF-18    | Upload résilient : multipart vers R2 via URL présignées, reprise au dernier segment confirmé, retry exponentiel (1 s → 5 min, illimité), file d'attente si plusieurs réunions                 | M         | Épreuve du crash réseau (section 24) réussie                                         |
| EF-19    | Mise à jour automatique silencieuse (Tauri updater, signature Ed25519), jamais pendant un enregistrement                                                                                      | M         | Une mise à jour publiée est déployée sur \> 90 % du parc sous 7 jours                |
| EF-20    | Journal local anonymisé et identifiant de diagnostic (debug_id) par réunion ; envoi du journal au support sur action explicite de l'utilisateur                                               | M         | Un ticket support contient le debug_id et le journal en un clic                      |
| EF-21    | Détection heuristique de réunion (audio système + micro actifs depuis 90 s, fenêtre Teams / Meet / Zoom au premier plan) avec proposition « Enregistrer cette réunion ? »                     | S         | Taux de fausses propositions \< 1 par jour en usage bureautique                      |
| EF-22    | Marquage d'une réunion comme privée (visible uniquement par son auteur et les Admins)                                                                                                         | S         | Un Member ne voit pas les réunions privées des autres                                |

10.3 Capture audio

| **Réf.** | **Exigence**                                                                                                                                                                                               | **Prio.** | **Critère d'acceptation**                                                  |
|----------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|----------------------------------------------------------------------------|
| EF-30    | Capture simultanée du microphone (WASAPI capture) et de l'audio système (WASAPI loopback sur le périphérique de rendu par défaut), rééchantillonnage 16 kHz mono, compensation de dérive d'horloge         | M         | Décalage micro / système \< 40 ms après 60 min (mesuré par signal de test) |
| EF-31    | Mixage en deux pistes conservées séparément jusqu'à l'encodage (piste locale, piste distante) puis encodage en un fichier Opus 32 kbps stéréo (gauche = local, droite = distant) pour aider la diarisation | M         | La diarisation distingue le locuteur local des distants dans 100 % des cas |
| EF-32    | Fonctionnement indépendant de l'application de réunion : Teams, Google Meet (navigateur), Zoom, WhatsApp Desktop, softphone, réunion physique (micro seul)                                                 | M         | Validé sur les 5 applications listées en recette                           |
| EF-33    | Pause / reprise sans créer de fichiers multiples ; le temps de pause n'est pas facturé                                                                                                                     | M         | Le quota consommé = durée effective d'enregistrement                       |
| EF-34    | Durée maximale d'une réunion : 4 h (Free : 60 min) ; avertissement à 5 min de la limite ; découpage automatique du traitement au-delà de 90 min                                                            | M         | Une réunion de 3 h produit un seul compte rendu cohérent                   |
| EF-35    | Gestion des casques Bluetooth en profil mains-libres (HFP, 8 kHz) : avertissement dans le widget et recommandation de basculer en A2DP ou en filaire                                                       | S         | L'avertissement apparaît sous 3 s après connexion du casque                |

10.4 Traitement et compte rendu

| **Réf.** | **Exigence**                                                                                                                                                                                                                                                                                   | **Prio.** | **Critère d'acceptation**                                                                               |
|----------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|---------------------------------------------------------------------------------------------------------|
| EF-40    | Pipeline asynchrone : dès l'upload confirmé, la réunion passe en file d'attente ; l'utilisateur n'attend jamais sur une requête HTTP                                                                                                                                                           | M         | Réponse HTTP 202 en \< 500 ms à la finalisation                                                         |
| EF-41    | Transcription avec détection automatique FR / EN (ou langue forcée par l'organisation), ponctuation, horodatage par segment, diarisation, résolution des noms de locuteurs quand ils sont prononcés (« merci Cabrel »)                                                                         | M         | WER ≤ 15 % sur le jeu de test FR/EN accents camerounais ; ≥ 85 % des segments attribués au bon locuteur |
| EF-42    | Extraction structurée : titre, participants, résumé (3 à 6 points), décisions, tâches (action, responsable, échéance normalisée, timestamp source, confiance)                                                                                                                                  | M         | Sur le jeu de test de non-hallucination, 0 décision ou tâche créée à partir d'une formulation vague     |
| EF-43    | Chaque décision et tâche est reliée à un timestamp de la transcription ; le clic renvoie au passage audio                                                                                                                                                                                      | M         | 100 % des éléments extraits ont une source cliquable                                                    |
| EF-44    | Délai de traitement : ≤ 5 min pour 1 h de réunion en charge normale ; état visible en temps réel (WebSocket) avec estimation                                                                                                                                                                   | M         | P95 du délai de traitement ≤ 6 min / heure d'audio                                                      |
| EF-45    | Fallback automatique : si le fournisseur de transcription échoue ou dépasse 10 min par heure d'audio, bascule vers le fournisseur secondaire ; si l'analyse LLM renvoie un JSON invalide, 3 tentatives avec prompt renforcé ; au-delà, état FAILED avec bouton « Relancer » et alerte Novafrik | M         | Taux de réunions FAILED \< 1 %                                                                          |
| EF-46    | Le compte rendu est produit dans la langue dominante de la réunion ; une traduction du résumé dans l'autre langue (FR ↔ EN) est disponible à la demande                                                                                                                                        | S         | Traduction disponible en \< 30 s                                                                        |
| EF-47    | Enregistrement du coût réel de chaque réunion (secondes STT, tokens LLM, fournisseur utilisé) dans le registre de consommation                                                                                                                                                                 | M         | Le coût par réunion est visible dans le back-office Novafrik                                            |

10.5 Interface web

| **Réf.** | **Exigence**                                                                                                                                                                                                                                                                                               | **Prio.** | **Critère d'acceptation**                                     |
|----------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|---------------------------------------------------------------|
| EF-50    | Tableau de bord : réunions du mois, heures consommées / quota, réunions en traitement, derniers comptes rendus, tâches ouvertes de l'utilisateur                                                                                                                                                           | M         | Chargement \< 2 s sur une connexion 3G (\< 500 Ko transférés) |
| EF-51    | Liste des réunions avec filtres (date, auteur, état, langue), tri, pagination ; suppression avec confirmation                                                                                                                                                                                              | M         | —                                                             |
| EF-52    | Lecteur de réunion : en-tête (titre éditable, date, durée, participants éditables), panneau gauche (résumé, décisions, tâches avec actions Valider / Rejeter / Éditer), panneau droit (transcription diarisée synchronisée avec le lecteur audio, clic sur un segment = saut audio), lecteur audio compact | M         | Tous les éléments sont éditables sans rechargement de page    |
| EF-53    | Édition du nom des locuteurs (« Speaker A » → « Marie ») propagée à toute la transcription et mémorisée pour les réunions suivantes de l'organisation (empreinte vocale non stockée : mémorisation par nom uniquement)                                                                                     | M         | —                                                             |
| EF-54    | Export PDF du compte rendu (avec ou sans transcription), au format de la section 12.4, avec filigrane sur le plan Free                                                                                                                                                                                     | M         | PDF généré en \< 5 s                                          |
| EF-55    | Recherche plein texte sur titres, résumés, décisions, tâches et transcriptions de l'organisation, avec extraits surlignés                                                                                                                                                                                  | S         | Résultats en \< 1 s sur 5 000 réunions                        |
| EF-56    | Partage d'un compte rendu par lien (lecture seule, expiration configurable, avec ou sans transcription et audio)                                                                                                                                                                                           | S         | —                                                             |
| EF-57    | Vue « Mes tâches » : toutes les tâches où l'utilisateur est responsable, avec état (à faire / fait) et lien vers la réunion source                                                                                                                                                                         | S         | —                                                             |
| EF-58    | Interface bilingue FR / EN, responsive (consultation sur mobile), accessible (contraste AA, navigation clavier)                                                                                                                                                                                            | M         | Audit Lighthouse accessibilité ≥ 90                           |

10.6 Notifications

| **Réf.** | **Exigence**                                                                                                                                  | **Prio.** | **Critère d'acceptation**                                      |
|----------|-----------------------------------------------------------------------------------------------------------------------------------------------|-----------|----------------------------------------------------------------|
| EF-60    | Notification desktop (Windows) et in-app à la fin du traitement, avec ouverture directe du compte rendu                                       | M         | —                                                              |
| EF-61    | Email « compte rendu prêt » (résumé + décisions + tâches en texte, lien vers le lecteur) à l'auteur ; option d'envoi aux participants membres | M         | Délivrabilité \> 98 % (domaine authentifié SPF / DKIM / DMARC) |
| EF-62    | Notifications de quota (70 %, 100 %), de renouvellement (J-3, J-1, J+1, J+3) et d'échec de traitement                                         | M         | —                                                              |
| EF-63    | Préférences de notification par utilisateur                                                                                                   | S         | —                                                              |

10.7 Facturation et quotas

| **Réf.** | **Exigence**                                                                                                                                                         | **Prio.** | **Critère d'acceptation**                                               |
|----------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|-------------------------------------------------------------------------|
| EF-70    | Plans et quotas conformes à la section 5.2 ; cycle de 30 jours à partir du paiement ; compteur de secondes traitées par organisation                                 | M         | Le quota est décrémenté à la seconde près à la fin de chaque traitement |
| EF-71    | Paiement Mobile Money (MTN MoMo, Orange Money) via Flutterwave : initiation du push USSD, suivi du statut, webhook signé, idempotence par identifiant de transaction | M         | Un webhook rejoué ne crédite jamais deux fois                           |
| EF-72    | Paiement par carte (Flutterwave) au Cameroun ; Stripe pour les marchés hors CEMAC (V1.1)                                                                             | M / S     | —                                                                       |
| EF-73    | Achat de packs d'heures ; changement de plan immédiat (upgrade au prorata, downgrade au cycle suivant)                                                               | M         | —                                                                       |
| EF-74    | Relances de renouvellement, période de grâce de 3 jours, mise en attente des traitements à J+4, rétrogradation à J+30 (section 9.4)                                  | M         | Le scénario P4 est reproductible en environnement de test               |
| EF-75    | Factures PDF numérotées séquentiellement, mentions légales camerounaises, TVA affichée lorsque applicable, historique des paiements                                  | M         | Conforme aux exigences de l'expert-comptable                            |
| EF-76    | Codes promotionnels et codes partenaires (attribution de la commission)                                                                                              | S         | —                                                                       |

10.8 Administration Novafrik (back-office)

| **Réf.** | **Exigence**                                                                                                                                                                                                                          | **Prio.** | **Critère d'acceptation**                                      |
|----------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|----------------------------------------------------------------|
| EF-80    | Liste des organisations : plan, consommation, état de paiement, dernière activité, score de santé (risque de churn si 0 réunion en 21 jours)                                                                                          | M         | —                                                              |
| EF-81    | Vue d'une réunion sans accès au contenu : états, durées, fournisseur, coûts, erreurs ; relance manuelle d'un traitement ; accès au contenu uniquement après consentement enregistré du client (ticket), tracé dans le journal d'audit | M         | Chaque accès au contenu par Novafrik est journalisé avec motif |
| EF-82    | Tableau de bord unit economics (section 5.9) avec export CSV                                                                                                                                                                          | M         | —                                                              |
| EF-83    | Gestion des fournisseurs : fournisseur STT et LLM par défaut, bascule manuelle, état du circuit breaker                                                                                                                               | M         | —                                                              |
| EF-84    | Remboursement / geste commercial, prolongation de cycle, ajustement manuel de quota (tracés)                                                                                                                                          | M         | —                                                              |

11\. Cycle de vie d'une réunion (machine à états)

L'état d'une réunion est partagé entre l'application desktop (états locaux) et le backend (états serveur). Chaque transition est journalisée avec horodatage et acteur. Les états sont les suivants :

> DESKTOP (local) BACKEND (serveur)
>
> ─────────────── ─────────────────
>
> IDLE
>
> │ Démarrer / Ctrl+Shift+R
>
> ▼
>
> RECORDING ◄──────────┐
>
> │ Pause │ Reprise
>
> ▼ │
>
> PAUSED ──────────────┘
>
> │ Terminer
>
> ▼
>
> FINALIZING_LOCAL (encodage Opus final, SHA-256, manifeste)
>
> │ POST /meetings/{id}/finalize-local ──────► CREATED
>
> ▼ │
>
> UPLOADING ──── réseau KO ──► UPLOAD_STALLED │ segments reçus
>
> │ ▲ │ retry ▼
>
> │ └──────────────────────────┘ UPLOADING
>
> │ tous segments confirmés + checksum OK │
>
> ▼ ▼
>
> UPLOADED ──────────────────────────────► QUEUED
>
> │ worker
>
> ▼
>
> TRANSCRIBING ── échec ──► FALLBACK_STT ──┐
>
> │ │
>
> ▼ ◄──────────────────────────────────┘
>
> ANALYZING ── JSON invalide ×3 ──► FAILED
>
> │ │ Relancer
>
> ▼ │
>
> COMPLETED ◄─────────────────────────┘
>
> │ notifications, quota décrémenté
>
> ▼
>
> PUBLISHED
>
> │ (purge audio à J+30/90)
>
> ▼
>
> AUDIO_PURGED
>
> États terminaux additionnels : CANCELLED (annulée avant upload), DELETED (supprimée par l'utilisateur),
>
> QUOTA_HOLD (upload terminé, traitement en attente de paiement).

| **Transition**              | **Déclencheur**                                    | **Effets**                                                                            | **Délai max**          |
|-----------------------------|----------------------------------------------------|---------------------------------------------------------------------------------------|------------------------|
| UPLOADED → QUEUED           | Checksum vérifié côté serveur                      | Message dans la file Redis, estimation de délai calculée                              | 10 s                   |
| QUEUED → TRANSCRIBING       | Worker disponible                                  | Appel au fournisseur STT, timeout = 10 min / h d'audio                                | 60 s en charge normale |
| TRANSCRIBING → FALLBACK_STT | Erreur 5xx, timeout ou circuit breaker ouvert      | Appel au fournisseur secondaire, incident tracé                                       | immédiat               |
| ANALYZING → FAILED          | 3 réponses LLM invalides ou erreur non récupérable | Alerte Novafrik (Sentry + email), bouton Relancer côté client, aucun quota décrémenté | —                      |
| COMPLETED → PUBLISHED       | Rapport persisté                                   | Quota décrémenté, registre de coût écrit, notifications envoyées, WebSocket « ready » | 5 s                    |
| QUOTA_HOLD → QUEUED         | Webhook de paiement confirmé                       | Traitement démarré automatiquement                                                    | 30 s                   |

12\. Spécifications UX / UI

12.1 Principes de conception

- **Zéro saisie avant valeur** : aucune configuration n'est requise avant la première réunion ; les réglages sont découverts progressivement.

- **Discrétion** : l'application desktop n'a pas de fenêtre principale pendant l'usage courant ; tout se passe depuis la zone de notification et le widget.

- **Confiance visible** : chaque élément généré par l'IA affiche son niveau de confiance (icône discrète) et sa source ; les éléments non validés sont visuellement distincts des éléments validés.

- **Faible bande passante** : pages web \< 500 Ko, images minimales, polices système, audio chargé à la demande par segments.

- **Bilingue natif** : toutes les chaînes en FR et EN dès le MVP ; la langue de l'interface est indépendante de la langue des réunions.

12.2 Écrans de l'application desktop

| **Écran**                            | **Contenu**                                                                                                                                                                                       | **Comportements**                                                                                          |
|--------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------|
| D-01 Connexion / liaison             | Champs email et mot de passe, ou code de liaison à 6 caractères, lien « Créer un compte » (ouvre le web)                                                                                          | Mémorisation de session ; message explicite en cas de version obsolète                                     |
| D-02 Menu de la zone de notification | Démarrer une réunion · Mes réunions (ouvre le web) · Test audio · Paramètres · Quitter ; ligne d'état (« Prêt », « Enregistrement 00:12:34 », « Envoi 63 % », « 2 réunions en attente réseau »)   | Clic gauche = Démarrer / Terminer ; clic droit = menu                                                      |
| D-03 Widget d'enregistrement         | Bandeau 280 × 56 px, coin supérieur droit, toujours au premier plan : point rouge pulsé, chronomètre, vu-mètres micro (vert) et système (bleu), boutons Pause et Terminer, poignée de déplacement | Réductible en pastille 24 px ; double-clic = restaurer ; alerte visuelle si un vu-mètre reste plat \> 20 s |
| D-04 Test audio                      | Trois étapes : lecture d'un son test (vérifie le loopback), phrase à prononcer (vérifie le micro), résultat avec recommandations                                                                  | Peut être relancé à tout moment ; résultat envoyé au support avec le debug_id si échec                     |
| D-05 Paramètres                      | Périphériques, raccourcis, démarrage avec Windows, détection de réunion (S), langue, dossier de stockage temporaire, envoi des journaux, version et mise à jour                                   | Aucun paramètre n'est requis pour fonctionner                                                              |

12.3 Écrans de l'interface web

| **Écran**                    | **Contenu**                                                                                                                                                                                                                                                                                                                                                                                                                                           | **Comportements**                                                                                                                                        |
|------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
| W-01 Inscription / connexion | Formulaire minimal, choix de langue, lien vers le téléchargement Windows après inscription                                                                                                                                                                                                                                                                                                                                                            | Redirection vers l'étape « Installer l'application » tant qu'aucune réunion n'a été traitée                                                              |
| W-02 Tableau de bord         | Bandeau de quota (barre, heures restantes, date de renouvellement), réunions en traitement avec progression, 5 derniers comptes rendus, mes tâches ouvertes                                                                                                                                                                                                                                                                                           | Bandeau d'alerte si paiement en retard ou quota atteint                                                                                                  |
| W-03 Réunions                | Tableau : date, titre, durée, auteur, langue, état, nombre de décisions / tâches ; filtres et recherche (S)                                                                                                                                                                                                                                                                                                                                           | Suppression avec confirmation ; sélection multiple (S)                                                                                                   |
| W-04 Lecteur                 | En-tête : titre éditable, date, durée, participants (puces éditables), badge d'état, boutons Exporter / Partager (S) / Supprimer. Panneau gauche (40 %) : Résumé, Décisions (cartes avec timestamp, Valider / Rejeter / Éditer), Tâches (tableau : action, responsable, échéance, source, Valider / Rejeter / Éditer). Panneau droit (60 %) : transcription en bulles par locuteur avec horodatage, lecteur audio en pied de page, surlignage karaoké | Clic sur un timestamp = lecture audio à cet instant ; renommage d'un locuteur propagé ; mode plein écran de la transcription ; mobile : panneaux empilés |
| W-05 Facturation             | Plan actuel, consommation, historique des paiements et factures, bouton « Payer par Mobile Money » (numéro enregistré, montant), packs, changement de plan                                                                                                                                                                                                                                                                                            | Statut du paiement en temps réel pendant la validation USSD (attente, succès, échec avec motif)                                                          |
| W-06 Organisation            | Membres et rôles, invitations, lexique métier, conservation audio, export / suppression des données                                                                                                                                                                                                                                                                                                                                                   | Actions destructives réservées à l'Owner avec confirmation par mot de passe                                                                              |
| W-07 Back-office Novafrik    | Organisations, réunions (métadonnées), fournisseurs, unit economics, partenaires (S)                                                                                                                                                                                                                                                                                                                                                                  | Accès restreint (rôle interne, 2FA obligatoire)                                                                                                          |

12.4 Format de référence du compte rendu

> NovaBrief · Réunion équipe marketing — stratégie éditoriale
>
> Samedi 6 septembre 2026 · 47 min · Participants : Junior, Cabrel, Marie, Paul
>
> RÉSUMÉ
>
> • L'équipe a revu la ligne éditoriale du mois prochain et arbitré les formats prioritaires.
>
> • Le calendrier de publication sera préparé avant le prochain point.
>
> • Le budget de sponsoring reste à confirmer avec la direction.
>
> DÉCISIONS
>
> ✓ La stratégie éditoriale est recentrée sur LinkedIn et YouTube. \[00:14:22\]
>
> ✓ Le calendrier de publication sera hebdomadaire. \[00:31:05\]
>
> TÂCHES
>
> ○ Préparer le plan éditorial Cabrel demain (07/09) \[00:17:32\]
>
> ○ Valider le calendrier de publication Junior vendredi (11/09) \[00:32:10\]
>
> ○ Demander le budget sponsoring (non attribué) — \[00:40:48\]
>
> TRANSCRIPTION ▸ afficher

Les éléments non attribués sont affichés comme tels (« non attribué »), jamais devinés. Les échéances relatives sont converties en dates absolues à partir de la date de la réunion, l'expression d'origine restant visible au survol.

13\. Gestion de l'incertitude de l'IA

La fiabilité perçue de NovaBrief dépend moins de la quantité d'informations extraites que de l'absence d'erreurs visibles. Le système applique trois niveaux de certitude, appliqués par le prompt et vérifiés par le jeu d'évaluation :

| **Niveau**                        | **Exemple de formulation**                                                                                  | **Traitement**                                                                                                                 |
|-----------------------------------|-------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------|
| Certain (confiance ≥ 0,8)         | « Cabrel, tu prépares le plan éditorial pour demain. » / « On valide le devis de 5 millions. »              | Tâche attribuée ou décision créée, affichée normalement                                                                        |
| Probable (0,5 ≤ confiance \< 0,8) | « Quelqu'un devrait préparer le plan éditorial. » / « On part plutôt sur le devis A, sauf avis contraire. » | Élément créé avec le marqueur « à confirmer », responsable vide si non nommé ; compte dans le score seulement après validation |
| Incertain (\< 0,5)                | « Il faudra voir pour le plan éditorial. » / « On pourrait regarder d'autres devis. »                       | Aucun élément créé ; la phrase reste dans le résumé si elle est structurante                                                   |

Deux garde-fous complètent ce mécanisme. D'une part, le modèle ne reçoit jamais la liste des membres de l'organisation comme candidats à l'attribution : un responsable n'est renseigné que s'il est nommé dans la transcription, puis rapproché des membres a posteriori par le backend (correspondance de prénom, confirmation par l'utilisateur en cas d'ambiguïté). D'autre part, chaque élément porte un timestamp source obligatoire ; un élément sans passage source identifiable est rejeté par la validation Pydantic avant même d'atteindre l'utilisateur.

14\. Exigences non fonctionnelles

| **Domaine**          | **Exigence**                                                                                                                                              | **Mesure**                                               |
|----------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------|
| Performance web      | Tableau de bord et lecteur chargés en \< 2 s sur 3G ; API P95 \< 300 ms hors traitement                                                                   | Lighthouse, métriques serveur                            |
| Performance desktop  | \< 80 Mo de RAM en enregistrement, \< 3 % CPU sur un portable i3 de 2018, \< 15 Mo d'installateur                                                         | Mesure en recette                                        |
| Délai de traitement  | ≤ 5 min par heure d'audio (P95 ≤ 6 min)                                                                                                                   | Métrique par réunion                                     |
| Disponibilité        | API et web : 99,5 % mensuel (≈ 3,6 h d'indisponibilité) ; l'enregistrement desktop n'est jamais affecté par une indisponibilité serveur                   | Uptime Kuma, page de statut                              |
| Résilience           | Aucune perte d'enregistrement en cas de coupure réseau, de crash applicatif ou d'arrêt brutal (perte max 5 s) ; reprise d'upload automatique              | Plan de tests section 24                                 |
| Scalabilité          | Architecture horizontale : workers et API sans état, file de traitement ; 20 000 h/mois sur le palier 2 sans refonte                                      | Test de charge 200 réunions simultanées                  |
| Sécurité             | Voir section 21 : TLS 1.3, chiffrement au repos, RLS, JWT courts, journal d'audit, secrets hors code                                                      | Revue de sécurité avant V1                               |
| Conformité           | Loi camerounaise n° 2024/017 (APDP) dès le MVP ; RGPD avant toute ouverture européenne                                                                    | Registre des traitements, DPO désigné                    |
| Internationalisation | FR et EN pour l'interface ; FR, EN et détection automatique pour les réunions ; devises XAF puis XOF, EUR                                                 | Fichiers de traduction complets, aucun texte codé en dur |
| Compatibilité        | Windows 10 (21H2+) et 11, x64 ; navigateurs : 2 dernières versions Chrome, Edge, Firefox, Safari ; mobile en consultation                                 | Matrice de test                                          |
| Observabilité        | Chaque réunion traçable de bout en bout par debug_id (desktop → API → worker → fournisseurs) ; journaux structurés ; alertes sur échecs et coûts anormaux | Recherche d'un debug_id en \< 1 min                      |
| Maintenabilité       | Monorepo, tests automatisés (couverture ≥ 70 % backend), CI bloquante, documentation d'architecture à jour (ADR)                                          | Revue à chaque version                                   |

**PARTIE III — SPÉCIFICATIONS TECHNIQUES**

*Cette partie est le contrat d'implémentation. Elle fixe l'architecture, les interfaces, le modèle de données, les règles de sécurité et les critères d'acceptation. Les extraits de code sont des références normatives pour les interfaces ; l'implémentation détaillée reste à la charge de l'agent IA de développement, dans le respect des décisions d'architecture (ADR) listées en 15.3.*

15\. Architecture générale

15.1 Vue d'ensemble

> ┌─────────────────────────────┐ ┌──────────────────────────────┐
>
> │ DESKTOP WINDOWS (Tauri) │ │ NAVIGATEUR (Nuxt 3 / Vue) │
>
> │ UI Vue + moteur audio Rust │ │ Dashboard, lecteur, billing │
>
> └──────────────┬──────────────┘ └──────────────┬───────────────┘
>
> │ HTTPS (JWT) · WebSocket statut │ HTTPS (JWT)
>
> ▼ ▼
>
> ┌───────────────────────────────────────────────────────────────────────┐
>
> │ API NovaBrief (FastAPI, Python 3.12) · OpenAPI 3.1 · /api/v1 │
>
> │ Auth · Organisations · Meetings · Reports · Billing · Admin · WS │
>
> └───────┬──────────────────────┬─────────────────────────┬──────────────┘
>
> │ │ │
>
> ▼ ▼ ▼
>
> ┌───────────────┐ ┌──────────────────┐ ┌───────────────────────┐
>
> │ PostgreSQL 16 │ │ Cloudflare R2 │ │ Redis (file + cache) │
>
> │ (RLS, pgvector│ │ audio Opus, │ │ │
>
> │ en V1.x) │ │ URL présignées │ └──────────┬────────────┘
>
> └───────────────┘ └──────────────────┘ │
>
> ▼
>
> ┌───────────────────────────────────┐
>
> │ WORKERS (Celery) │
>
> │ transcribe → analyze → publish │
>
> │ purge audio · relances · métriques│
>
> └──────┬───────────────┬────────────┘
>
> │ │
>
> TranscriptionProvider LLMProvider
>
> ┌──────┴──────┐ ┌────┴─────────┐
>
> │ AssemblyAI │ │ OpenAI │
>
> │ Deepgram │ │ GPT-5 mini │
>
> │ Whisper (P3)│ │ (autre P2) │
>
> └─────────────┘ └──────────────┘
>
> BillingProvider
>
> ┌──────┴──────────┐
>
> │ Flutterwave │
>
> │ Stripe (V1.1) │
>
> └─────────────────┘

15.2 Choix de la pile technique et alternatives écartées

| **Composant**      | **Choix**                                                                            | **Alternatives considérées**               | **Justification**                                                                                                                                                                              |
|--------------------|--------------------------------------------------------------------------------------|--------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Client desktop     | Tauri 2 + Rust (moteur audio) + Vue (UI)                                             | Electron ; .NET WPF ; Flutter              | Binaire \< 15 Mo, RAM \< 80 Mo, accès natif WASAPI en Rust, updater signé intégré. Electron : 150 Mo et RAM ×3 ; .NET : excellent sur Windows mais ferme la porte à macOS et duplique l'UI web |
| Capture audio      | WASAPI (capture + loopback) via la crate windows-rs                                  | Pilotes virtuels (VB-Cable), hooks         | Aucune installation de pilote, aucun droit administrateur, fonctionne avec toutes les applications                                                                                             |
| Codec              | Opus 32 kbps (libopus via crate audiopus)                                            | MP3, AAC, WAV                              | 14 Mo/h, qualité voix optimale à 16 kHz, standard ouvert, accepté par tous les fournisseurs STT                                                                                                |
| Backend            | FastAPI, Python 3.12, SQLAlchemy 2 async, Pydantic v2                                | NestJS (Node) ; Django                     | Écosystème IA (SDK fournisseurs, évaluation) ; typage Pydantic partagé avec les schémas LLM ; async natif                                                                                      |
| File de traitement | Celery 5 + Redis                                                                     | RQ ; Dramatiq ; SQS                        | Mature, retries, beat pour les tâches planifiées (purge, relances)                                                                                                                             |
| Base de données    | PostgreSQL 16 auto-hébergé (Hetzner) avec RLS                                        | Supabase (25 \$/mois) ; MySQL              | Coût nul, RLS natif pour l'isolation multi-tenant, pgvector disponible pour la recherche sémantique V1.x. Supabase reste une option de repli si l'exploitation s'avère trop lourde             |
| Stockage objet     | Cloudflare R2                                                                        | S3 ; Backblaze B2 ; Hetzner Object Storage | Egress gratuit (les workers lisent l'audio sans frais), 10 Go gratuits, API S3                                                                                                                 |
| Frontend web       | Nuxt 3 (Vue 3, TypeScript), Tailwind                                                 | Next.js ; SvelteKit                        | Cohérence avec l'UI Tauri (même composants Vue), SSR pour le SEO des pages publiques                                                                                                           |
| Infrastructure     | Hetzner Cloud (ARM CAX), Docker Compose, Caddy (TLS auto)                            | Kubernetes ; AWS / GCP                     | Coût ×4 inférieur, simplicité d'exploitation pour un agent IA ; Kubernetes seulement au palier 3                                                                                               |
| Email              | Resend (gratuit puis 20 \$)                                                          | SES ; Postmark ; Brevo                     | API simple, DKIM géré, gratuit jusqu'à 3 000 mails/mois                                                                                                                                        |
| Observabilité      | Sentry (erreurs), Prometheus + Grafana, Uptime Kuma, journaux structurés (structlog) | Datadog                                    | Coût nul ou quasi nul, auto-hébergé                                                                                                                                                            |
| Signature de code  | Azure Trusted Signing (≈ 10 \$/mois)                                                 | Certificat OV/EV (300-600 €/an)            | Indispensable pour SmartScreen ; moins cher et sans clé matérielle                                                                                                                             |

15.3 Décisions d'architecture (ADR) à respecter

1.  **ADR-01 — Aucune dépendance directe à un fournisseur d'IA.** Tout appel de transcription passe par TranscriptionProvider, tout appel LLM par LLMProvider, tout paiement par BillingProvider. Un fournisseur se change par configuration, sans modifier le code métier.

2.  **ADR-02 — Séparation stricte transcription / intelligence.** On n'envoie jamais l'audio à un LLM. Le pipeline est audio → transcription horodatée → analyse structurée → JSON validé → présentation.

3.  **ADR-03 — Le LLM renvoie un JSON validé par schéma, jamais du texte libre.** Toute sortie est validée par Pydantic ; un échec de validation est une erreur, pas un contenu.

4.  **ADR-04 — Isolation multi-tenant par Row-Level Security PostgreSQL** en complément des filtres applicatifs : la variable de session app.current_org_id est positionnée à chaque transaction ; aucune requête ne peut lire une autre organisation.

5.  **ADR-05 — L'enregistrement est local et chiffré ; le réseau est optionnel.** Le desktop ne dépend du backend que pour l'upload et le statut.

6.  **ADR-06 — L'audio est éphémère par défaut** (30 jours, 90 en Business), les textes sont durables. La purge est un job planifié et audité.

7.  **ADR-07 — Chaque réunion porte un \`debug_id\` de bout en bout** (desktop, API, workers, appels fournisseurs) pour le support.

8.  **ADR-08 — Le coût réel de chaque réunion est enregistré** (usage_ledger) : le tableau de bord d'unit economics est une fonctionnalité du MVP.

9.  **ADR-09 — Prix et devises sont des données, pas du code** : table plans par marché (XAF, XOF, EUR), jamais de montant codé en dur.

10. **ADR-10 — Monorepo** (apps/desktop, apps/web, apps/api, packages/schemas) avec les schémas JSON partagés générés depuis Pydantic vers TypeScript.

16\. Application desktop (Tauri + Rust)

16.1 Structure du projet

> apps/desktop/
>
> ├── src-tauri/ \# Rust
>
> │ ├── src/main.rs \# commandes Tauri, tray, raccourcis, updater
>
> │ ├── crates/audio-engine/ \# capture WASAPI, resampling, drift, mix, Opus
>
> │ ├── crates/vault/ \# chiffrement local AES-256-GCM, DPAPI
>
> │ ├── crates/uploader/ \# multipart R2, reprise, file d'attente
>
> │ └── crates/api-client/ \# client HTTP typé (généré depuis OpenAPI)
>
> └── src/ \# Vue 3 : widget, test audio, paramètres

16.2 Moteur audio

Le moteur tourne dans un thread dédié à priorité temps réel (MMCSS « Pro Audio »). Deux clients WASAPI en mode partagé sont ouverts : capture sur le périphérique d'entrée par défaut, loopback sur le périphérique de rendu par défaut. Chaque flux est converti en mono 32 bits flottant, rééchantillonné à 16 kHz (rubato, qualité « sinc » ), puis aligné sur l'horloge système par un compensateur de dérive (mesure du décalage cumulé toutes les 10 s, correction par insertion / suppression d'échantillons interpolés sous 1 ms). Les deux pistes sont encodées ensemble en Opus stéréo 32 kbps (gauche = micro, droite = système), par trames de 20 ms, et écrites en segments de 5 secondes dans un conteneur Ogg chiffré.

> Micro (WASAPI capture) ─► resample 16 kHz ─► mono ─┐
>
> ├─► compensateur de dérive
>
> Système (WASAPI loopback) ─► resample 16 kHz ─► mono ─┘ │
>
> ▼
>
> Opus stéréo 32 kbps (G = micro, D = système)
>
> │
>
> ▼
>
> segments Ogg 5 s · AES-256-GCM · flush disque
>
> │
>
> ▼
>
> manifeste (SHA-256 par segment, durée, périphériques)

Cas particuliers à traiter explicitement : périphérique de rendu sans flux actif (le loopback ne délivre pas de paquets tant que rien n'est joué : injecter un flux silencieux de maintien) ; changement de périphérique par défaut en cours de route (réouverture transparente sur l'événement IMMNotificationClient) ; formats exclusifs ou débits inhabituels (44,1 / 48 / 96 kHz, 1 à 8 canaux) ; casques Bluetooth en profil mains-libres (avertissement) ; écho quand l'utilisateur est sur haut-parleurs (mitigé par la séparation des pistes, pas d'AEC en V1).

16.3 Stockage local et reprise

Les segments sont écrits dans %LOCALAPPDATA%\NovaBrief\recordings\\meeting_id}\\ avec un manifeste JSON mis à jour à chaque segment. La clé de chiffrement de la réunion est générée aléatoirement, chiffrée par une clé de compte dérivée du refresh token et protégée par DPAPI. Au démarrage, l'application recherche les réunions en état RECORDING (crash) ou UPLOADING et reprend : une réunion interrompue par un crash est finalisée avec les segments présents et signalée à l'utilisateur. Les fichiers locaux sont supprimés 24 h après confirmation de l'upload (72 h si le traitement a échoué).

16.4 Upload résilient

À la finalisation, le desktop appelle POST /meetings/{id}/finalize-local avec le manifeste ; l'API renvoie des URL présignées R2 (multipart, une part par groupe de segments ≈ 5 Mo). Chaque part confirmée est enregistrée localement ; en cas d'échec, reprise à la première part non confirmée avec retry exponentiel (1 s, 2 s, 4 s … 5 min, sans limite). Lorsque toutes les parts sont confirmées, POST /meetings/{id}/finalize transmet le SHA-256 global ; le serveur vérifie l'intégrité avant de mettre en file. Les uploads sont limités à 2 réunions simultanées et à 80 % de la bande passante montante mesurée pour ne pas dégrader la réunion suivante.

16.5 Mise à jour, signature, télémétrie

- Mise à jour via l'updater Tauri (manifeste signé Ed25519, canal stable / bêta), téléchargement en arrière-plan, application au prochain démarrage ou sur demande, jamais pendant un enregistrement.

- Binaire et installateur signés via Azure Trusted Signing dans la CI ; vérification SmartScreen dans la recette de chaque version.

- Télémétrie minimale et anonyme (version, OS, succès / échec d'enregistrement, durée d'upload) avec opt-out ; aucun contenu audio ou texte n'est jamais envoyé hors du flux de traitement.

17\. Backend et API

17.1 Contrat d'interface (extrait normatif)

| **Méthode**  | **Endpoint**                          | **Description**                                                           | **Auth**       |
|--------------|---------------------------------------|---------------------------------------------------------------------------|----------------|
| POST         | /api/v1/auth/register                 | Création de compte et d'organisation                                      | —              |
| POST         | /api/v1/auth/token                    | Connexion (email / téléphone + mot de passe) → access + refresh           | —              |
| POST         | /api/v1/auth/refresh · /logout        | Rotation du refresh token · révocation                                    | Refresh        |
| POST         | /api/v1/auth/device-link              | Liaison d'un poste par code à 6 caractères                                | User           |
| GET/PATCH    | /api/v1/me · /organizations/current   | Profil, organisation, lexique, conservation                               | User / Admin   |
| POST/DELETE  | /api/v1/organizations/current/members | Invitation, rôle, révocation                                              | Admin          |
| POST         | /api/v1/meetings                      | Déclaration d'une réunion (début d'enregistrement) → meeting_id, debug_id | User           |
| POST         | /api/v1/meetings/{id}/finalize-local  | Manifeste local → URL présignées multipart                                | User           |
| POST         | /api/v1/meetings/{id}/finalize        | Checksum global, durée, version client → 202 QUEUED ou QUOTA_HOLD         | User           |
| GET          | /api/v1/meetings · /meetings/{id}     | Liste filtrée · rapport, transcription, segments, URL audio temporaire    | Member         |
| PATCH/DELETE | /api/v1/meetings/{id}                 | Titre, participants, privé · suppression                                  | Auteur / Admin |
| POST         | /api/v1/meetings/{id}/retry           | Relance d'un traitement FAILED                                            | Auteur / Admin |
| PATCH        | /api/v1/decisions/{id} · /tasks/{id}  | Validation, rejet, édition (feedback)                                     | Member         |
| PATCH        | /api/v1/meetings/{id}/speakers/{tag}  | Renommage d'un locuteur                                                   | Member         |
| GET          | /api/v1/meetings/{id}/export.pdf      | Export PDF                                                                | Member         |
| GET          | /api/v1/search?q=                     | Recherche plein texte (V1.1)                                              | Member         |
| GET          | /api/v1/billing/usage · /plans        | Quota, consommation, cycle · plans du marché                              | Member / —     |
| POST         | /api/v1/billing/checkout              | Initiation du paiement (plan ou pack, méthode momo / card) → statut       | Owner          |
| GET          | /api/v1/billing/payments/{id}         | Statut d'un paiement en cours                                             | Owner          |
| POST         | /api/v1/billing/webhooks/flutterwave  | Webhook signé (verif-hash) + vérification serveur à serveur               | HMAC           |
| WS           | /api/v1/ws/meetings/{id}              | Statut du traitement en temps réel (ticket WS à usage unique)             | Ticket         |
| \*           | /api/v1/admin/\*\*                    | Back-office Novafrik                                                      | Interne + 2FA  |

17.2 Exemple de contrat : finalisation

> POST /api/v1/meetings/7c9e6679-7425-40de-944b-e07fc1f90ae7/finalize
>
> { "duration_seconds": 2832, "paused_seconds": 60, "audio_sha256": "e3b0c442…",
>
> "client_version": "1.0.4", "language_hint": null, "devices": {"input": "Realtek…", "output": "Jabra…"} }
>
> HTTP 202 Accepted
>
> { "meeting_id": "7c9e6679-…", "status": "QUEUED", "debug_id": "DBG-DLA-20260906-8842",
>
> "estimated_seconds": 180, "quota": {"used_seconds": 45120, "limit_seconds": 90000},
>
> "ws_ticket": "wst\_…" }
>
> HTTP 402 Payment Required (quota atteint : audio conservé, traitement en attente)
>
> { "status": "QUOTA_HOLD", "checkout_url": "https://app.novabrief.com/billing?pack=5h" }

17.3 Règles transverses

- **Authentification** : mots de passe hachés Argon2id ; access token JWT 15 min (RS256, claims : sub, org, role, ver) ; refresh token opaque 30 jours avec rotation et détection de réutilisation ; 2FA TOTP optionnelle (obligatoire pour le back-office).

- **Autorisation** : dépendance FastAPI qui positionne SET LOCAL app.current_org_id dans chaque transaction ; contrôles de rôle explicites par endpoint ; les ressources sont adressées par UUID v7.

- **Idempotence** : en-tête Idempotency-Key obligatoire sur les POST de création et de paiement (conservation 24 h).

- **Limitation de débit** : par IP et par compte (auth : 10 / min ; API : 600 / min) ; réponses 429 avec Retry-After.

- **Erreurs** : format Problem Details (RFC 9457) avec debug_id ; messages traduits FR / EN côté client à partir de codes stables.

- **Versionnement** : /api/v1 gelé à la V1 ; les changements incompatibles créent /api/v2 ; le desktop annonce sa version et reçoit 426 si elle n'est plus supportée.

- **Quotas** : décrément atomique à la publication (UPDATE … WHERE consumed + dur \<= limit) ; les secondes de pause ne comptent pas ; arrondi à la seconde.

18\. Pipeline d'intelligence artificielle

18.1 Interfaces normatives

> \# packages/ai/transcription/base.py
>
> class Utterance(BaseModel):
>
> speaker: str \# "A", "B"… puis nom résolu
>
> start_ms: int
>
> end_ms: int
>
> text: str
>
> confidence: float
>
> channel: Literal\["local", "remote", "mixed"\] \| None = None
>
> class TranscriptionResult(BaseModel):
>
> language: str \# "fr" \| "en"
>
> utterances: list\[Utterance\]
>
> duration_seconds: int
>
> provider: str \# "assemblyai:universal-3.5" \| "deepgram:nova-3" \| "whisper:large-v3"
>
> cost_usd: Decimal \# coût facturé, écrit dans usage_ledger
>
> class TranscriptionProvider(Protocol):
>
> async def transcribe(self, audio_url: str, \*, language_hint: str \| None,
>
> keyterms: list\[str\], stereo_channels: bool) -\> TranscriptionResult: ...
>
> async def health(self) -\> bool: ...
>
> \# packages/ai/llm/base.py
>
> class LLMProvider(Protocol):
>
> async def extract(self, system: str, user: str, schema: type\[BaseModel\],
>
> \*, temperature: float = 0.1) -\> tuple\[BaseModel, Usage\]: ...

Le routeur TranscriptionRouter applique un disjoncteur (circuit breaker) par fournisseur : ouvert après 5 échecs en 10 minutes ou un taux d'erreur 5xx \> 5 %, demi-ouvert après 2 minutes. L'ordre de préférence est une configuration (assemblyai, deepgram), modifiable depuis le back-office sans redéploiement. AssemblyAI est appelé avec speaker_labels, language_detection (ou language_code si l'organisation force la langue), multichannel lorsque l'audio est stéréo (piste locale / distante) et keyterms_prompt alimenté par le lexique de l'organisation.

18.2 Étapes du traitement

1.  **Préparation** : téléchargement de l'audio depuis R2 (URL présignée 15 min), vérification du checksum, lecture du manifeste (durée, pauses, périphériques).

2.  **Transcription + diarisation** via le routeur ; normalisation des locuteurs (A, B, C…) ; fusion des micro-segments \< 1 s ; détection de la langue dominante.

3.  **Résolution des locuteurs** : recherche dans la transcription des adresses nominatives (« merci Cabrel », « Marie, tu peux… ») pour proposer un nom par locuteur avec un score ; le locuteur du canal local est proposé comme l'auteur de la réunion.

4.  **Découpage** : au-delà de 90 minutes, la transcription est découpée en fenêtres de 60 minutes avec 5 minutes de recouvrement ; chaque fenêtre est analysée séparément puis une passe de consolidation fusionne résumé, décisions et tâches (déduplication par similarité).

5.  **Extraction structurée** (prompt 18.3, schéma 18.4) avec response_format JSON et validation Pydantic ; 3 tentatives maximum avec un prompt de correction citant l'erreur de validation.

6.  **Post-traitement** : normalisation des échéances (« vendredi prochain » → date ISO à partir de la date de la réunion et du fuseau de l'organisation), rapprochement des responsables avec les membres (prénom exact, sinon suggestion), calcul de la confiance, vérification que chaque timestamp source existe dans la transcription.

7.  **Publication** : écriture transactionnelle (rapport, décisions, tâches, segments), décrément du quota, écriture du coût dans usage_ledger, notifications, événement WebSocket.

18.3 Prompt système de référence (version 1.0, français)

> Tu es le moteur d'analyse de NovaBrief. Tu reçois la transcription horodatée et diarisée
>
> d'une réunion d'entreprise (français ou anglais, contexte africain francophone / anglophone).
>
> Tu produis UNIQUEMENT un objet JSON conforme au schéma fourni. Aucun texte hors du JSON.
>
> RÈGLES ABSOLUES
>
> 1\. Fidélité : n'extrais que ce qui est dit. N'invente jamais un fait, une décision, une tâche,
>
> un responsable ou une date.
>
> 2\. Décision = arbitrage explicitement validé (« on valide », « c'est décidé », « on part sur »,
>
> accord clair de la personne qui décide). Une idée, une hypothèse ou un débat non conclu
>
> n'est PAS une décision.
>
> 3\. Tâche = action concrète confiée à quelqu'un ou clairement engagée. Le champ assignee ne
>
> contient un nom que s'il est prononcé dans la transcription ; sinon null. Ne déduis jamais
>
> un responsable.
>
> 4\. Échéance : recopie l'expression d'origine dans deadline_text (« vendredi », « avant le 15 »)
>
> ; ne calcule pas de date.
>
> 5\. Chaque décision et chaque tâche porte source_start_ms : le début de l'échange d'où elle
>
> provient, pris dans les horodatages fournis.
>
> 6\. Confiance : 0,9-1,0 si formulation explicite et validée ; 0,5-0,8 si probable ; \< 0,5 =
>
> ne pas extraire.
>
> 7\. Respecte les termes locaux (FCFA, DGI, CNPS, MoMo, bon de commande, PV, chef de service…).
>
> 8\. Résumé : 3 à 6 points, factuels, au passé composé, sans opinion, dans la langue dominante.
>
> 9\. Titre : 4 à 10 mots, sans « réunion du … », reflétant l'objet principal.
>
> 10\. Participants : uniquement les personnes nommées ou se présentant dans la conversation.
>
> EXEMPLES
>
> « Cabrel, tu prépares le plan éditorial pour demain. » → tâche, assignee « Cabrel »,
>
> deadline_text « demain », confidence 0.95
>
> « Il faudrait qu'on améliore notre communication. » → rien (intention vague)
>
> « On valide le devis de 5 millions. » → décision, confidence 0.95
>
> « On pourrait regarder d'autres devis. » → rien (débat non conclu)

18.4 Schéma de sortie (Pydantic v2, source de vérité)

> class Decision(BaseModel):
>
> content: str = Field(min_length=8, max_length=400)
>
> source_start_ms: int = Field(ge=0)
>
> confidence: float = Field(ge=0.5, le=1.0)
>
> class Task(BaseModel):
>
> action: str = Field(min_length=5, max_length=300) \# verbe à l'infinitif
>
> assignee: str \| None = None \# nom prononcé, sinon None
>
> deadline_text: str \| None = None
>
> source_start_ms: int = Field(ge=0)
>
> confidence: float = Field(ge=0.5, le=1.0)
>
> class MeetingReport(BaseModel):
>
> title: str = Field(min_length=4, max_length=120)
>
> language: Literal\["fr", "en"\]
>
> participants: list\[str\] = Field(max_length=30)
>
> summary: list\[str\] = Field(min_length=1, max_length=6)
>
> decisions: list\[Decision\] = Field(max_length=40)
>
> tasks: list\[Task\] = Field(max_length=60)
>
> @model_validator(mode="after")
>
> def sources_must_exist(self, info): \# chaque source_start_ms doit tomber dans un segment
>
> ...

18.5 Paramètres et coûts

Modèle par défaut : GPT-5 mini, température 0,1, sortie JSON forcée, 16 000 tokens d'entrée et 2 000 de sortie par heure de réunion en moyenne. Le titre, les participants et la traduction du résumé peuvent être délégués à GPT-5 nano après évaluation. Tous les appels enregistrent tokens, latence, fournisseur et coût dans usage_ledger. Un plafond de coût par réunion (0,50 \$) déclenche une alerte et interrompt les relances.

18.6 Évaluation continue et NovaBrief Score

Un jeu d'évaluation de 50 réunions réelles annotées (25 FR, 25 EN, accents camerounais, 15 à 90 minutes, réunions physiques et Teams / Meet) est constitué pendant la bêta, avec le consentement écrit des participants. Pour chaque réunion, les décisions et tâches attendues sont annotées manuellement, ainsi que 10 formulations vagues qui ne doivent rien produire. Chaque modification du prompt, du modèle ou du fournisseur est évaluée sur ce jeu avant déploiement : précision et rappel des décisions et des tâches, exactitude des responsables, taux d'hallucination (éléments produits à partir de formulations vagues, objectif 0), WER de la transcription, exactitude de la diarisation. Le NovaBrief Score en production est calculé à partir des validations / rejets des utilisateurs (objectif ≥ 85 %) et affiché dans le back-office.

19\. Modèle de données

19.1 Entités

| **Table**           | **Rôle**                             | **Colonnes clés**                                                                                                                                                                                                                                               |
|---------------------|--------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| organizations       | Tenant                               | id, name, legal_id, market (CM / CI / FR…), plan_id, cycle_start, cycle_end, quota_seconds, consumed_seconds, audio_retention_days, default_language, status (active / grace / hold / free_fallback), momo_number, created_at                                   |
| users               | Comptes                              | id, organization_id, email, phone, password_hash, full_name, role, locale, timezone, email_verified_at, totp_secret, created_at                                                                                                                                 |
| devices             | Postes desktop liés                  | id, user_id, name, os_version, app_version, last_seen_at, refresh_token_hash                                                                                                                                                                                    |
| meetings            | Réunions                             | id, organization_id, created_by, title, status, language, started_at, duration_seconds, paused_seconds, audio_r2_key, audio_sha256, audio_bytes, is_private, debug_id, provider_stt, provider_llm, purge_at, purged_at, failed_reason, created_at, completed_at |
| transcripts         | Texte intégral                       | id, meeting_id, organization_id, language, raw_text, provider_metadata (jsonb), tsv (tsvector)                                                                                                                                                                  |
| transcript_segments | Segments diarisés                    | id, transcript_id, organization_id, speaker_tag, speaker_name, start_ms, end_ms, text, confidence, channel                                                                                                                                                      |
| reports             | Compte rendu                         | id, meeting_id, organization_id, title, participants (jsonb), summary (jsonb), model_version, prompt_version, generated_at                                                                                                                                      |
| decisions / tasks   | Éléments extraits                    | id, meeting_id, organization_id, content / action, assignee_name, assignee_user_id, deadline_text, deadline_date, source_start_ms, confidence, human_status (UNREVIEWED / APPROVED / REJECTED / EDITED), edited_content, reviewed_by, reviewed_at               |
| plans / prices      | Catalogue                            | plan_code, market, currency, amount, quota_seconds, retention_days, features (jsonb), active                                                                                                                                                                    |
| subscriptions       | Abonnement courant et historique     | id, organization_id, plan_code, started_at, ends_at, status, source_payment_id                                                                                                                                                                                  |
| payments            | Transactions                         | id, organization_id, provider, provider_ref, method (momo / card), amount, currency, status, raw_webhook (jsonb), idempotency_key, created_at, confirmed_at                                                                                                     |
| invoices            | Factures                             | id, organization_id, number (séquentiel), payment_id, amount_ht, vat_amount, amount_ttc, pdf_r2_key, issued_at                                                                                                                                                  |
| usage_ledger        | Registre de consommation et de coûts | id, organization_id, meeting_id, seconds_billed, stt_provider, stt_cost_usd, llm_tokens_in, llm_tokens_out, llm_cost_usd, storage_cost_usd, recorded_at                                                                                                         |
| audit_log           | Traçabilité                          | id, organization_id, actor_id, actor_type (user / system / novafrik), action, target_type, target_id, ip, reason, created_at                                                                                                                                    |
| notifications       | File de notifications                | id, user_id, type, channel, payload, sent_at, read_at                                                                                                                                                                                                           |

19.2 Règles d'intégrité et de sécurité

- Toutes les tables porteuses de contenu ont une colonne organization_id (dénormalisée) et une politique RLS USING (organization_id = current_setting('app.current_org_id')::uuid). Le rôle applicatif n'a pas le droit BYPASSRLS ; les workers positionnent l'organisation de la réunion traitée.

- Clés primaires UUID v7 (triables), horodatages timestamptz, montants en numeric(12,2), coûts en numeric(10,5).

- Index : meetings (organization_id, created_at desc), transcript_segments (transcript_id, start_ms), tasks (organization_id, assignee_user_id, human_status), transcripts using gin (tsv), payments (provider, provider_ref) unique.

- Suppression d'une réunion : ON DELETE CASCADE sur transcript, segments, rapport, décisions, tâches ; usage_ledger conservé (meeting_id mis à null) pour la comptabilité.

- Migrations Alembic versionnées, appliquées par la CI ; aucune modification manuelle en production.

19.3 Extrait DDL de référence

> CREATE TABLE meetings (
>
> id uuid PRIMARY KEY,
>
> organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
>
> created_by uuid NOT NULL REFERENCES users(id),
>
> title varchar(200),
>
> status varchar(24) NOT NULL DEFAULT 'CREATED',
>
> language varchar(5),
>
> started_at timestamptz NOT NULL,
>
> duration_seconds int NOT NULL DEFAULT 0,
>
> paused_seconds int NOT NULL DEFAULT 0,
>
> audio_r2_key varchar(512),
>
> audio_sha256 char(64),
>
> is_private boolean NOT NULL DEFAULT false,
>
> debug_id varchar(40) NOT NULL UNIQUE,
>
> provider_stt varchar(40),
>
> purge_at timestamptz,
>
> purged_at timestamptz,
>
> created_at timestamptz NOT NULL DEFAULT now(),
>
> completed_at timestamptz
>
> );
>
> ALTER TABLE meetings ENABLE ROW LEVEL SECURITY;
>
> CREATE POLICY org_isolation ON meetings FOR ALL
>
> USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
>
> CREATE INDEX meetings_org_created ON meetings (organization_id, created_at DESC);
>
> CREATE TABLE usage_ledger (
>
> id uuid PRIMARY KEY,
>
> organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
>
> meeting_id uuid REFERENCES meetings(id) ON DELETE SET NULL,
>
> seconds_billed int NOT NULL,
>
> stt_provider varchar(40) NOT NULL,
>
> stt_cost_usd numeric(10,5) NOT NULL,
>
> llm_tokens_in int NOT NULL, llm_tokens_out int NOT NULL,
>
> llm_cost_usd numeric(10,5) NOT NULL,
>
> recorded_at timestamptz NOT NULL DEFAULT now()
>
> );

20\. Facturation et paiements

20.1 Abstraction BillingProvider

BillingProvider expose create_checkout(org, item, method) → CheckoutSession, verify(provider_ref) → PaymentStatus et parse_webhook(headers, body) → WebhookEvent. Deux implémentations : FlutterwaveProvider (Cameroun : Mobile Money MTN / Orange, cartes) et StripeProvider (V1.1, marchés hors CEMAC). Le choix se fait par le marché de l'organisation. Aucun montant n'est calculé dans le code : les prix viennent de la table prices.

20.2 Flux Mobile Money

> Owner (web) API NovaBrief Flutterwave Téléphone (MTN / Orange)
>
> │ 1. Payer Team 10 000 │ │ │
>
> ├──────────────────────►│ 2. POST /charges │ │
>
> │ │ (mobile_money_franco, │ │
>
> │ │ tx_ref = payment.id) │ │
>
> │ ├────────────────────────►│ 3. push USSD │
>
> │ 4. « Validez sur │ ├─────────────────────────►│
>
> │ votre téléphone » │ │ │ 5. PIN
>
> │◄──────────────────────┤ │◄─────────────────────────┤
>
> │ (polling statut 3 s)│ 6. webhook charge.completed (verif-hash) │
>
> │ │◄────────────────────────┤ │
>
> │ │ 7. GET /transactions/{id}/verify (contrôle serveur)│
>
> │ ├────────────────────────►│ │
>
> │ │ 8. payment CONFIRMED, subscription renouvelée, │
>
> │ 9. « Paiement reçu » │ facture PDF, quota réinitialisé, audit_log │
>
> │◄──────────────────────┤ │ │

- Le webhook est authentifié par l'en-tête verif-hash (secret partagé) **et** confirmé par un appel de vérification serveur à serveur avant tout crédit : un webhook seul ne suffit jamais.

- Idempotence : payments.provider_ref est unique ; un webhook rejoué renvoie 200 sans effet.

- Le montant et la devise du webhook sont comparés à ceux attendus ; tout écart met le paiement en état REVIEW avec alerte.

- Réconciliation quotidienne : liste des transactions Flutterwave du jour comparée à la table payments ; écarts signalés dans le back-office.

- Frais : 2 % Mobile Money, 4,8 % cartes, 1 500 FCFA par virement bancaire ; les virements sont hebdomadaires.

20.3 Cycle d'abonnement et relances

| **Moment** | **Action système**                                                                                          | **Canal**                                  |
|------------|-------------------------------------------------------------------------------------------------------------|--------------------------------------------|
| J-3        | Rappel avec lien de paiement pré-rempli                                                                     | Email + notification desktop + bandeau web |
| J-1        | Second rappel                                                                                               | Email + notification desktop               |
| J0 → J+3   | Période de grâce : service complet, bandeau « paiement attendu »                                            | Bandeau web + desktop                      |
| J+4        | Nouveaux traitements en QUOTA_HOLD (enregistrement toujours possible), comptes rendus existants accessibles | Email + desktop                            |
| J+10       | Relance avec proposition de plan inférieur                                                                  | Email                                      |
| J+30       | Rétrogradation en Free, données conservées 90 jours                                                         | Email                                      |
| J+120      | Suppression des données après deux avertissements (J+90, J+113)                                             | Email                                      |

20.4 Factures et TVA

Chaque paiement confirmé génère une facture PDF numérotée (série annuelle NB-2027-000001), avec l'identité de Novafrik (RCCM, NIU), celle du client, le détail HT / TVA / TTC lorsque la TVA est applicable, et le mode de paiement. Le paramètre vat_applicable de l'éditeur est activé par Novafrik lors du passage au régime du réel ; les prix affichés restent TTC et la ventilation apparaît sur la facture. Les factures sont stockées dans R2 et accessibles depuis W-05.

21\. Sécurité et conformité

21.1 Modèle de menace (résumé)

| **Actif**               | **Menace**                                                                   | **Contrôle**                                                                                                                                                                      |
|-------------------------|------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Audio et transcriptions | Accès par une autre organisation ; fuite via stockage ; accès interne abusif | RLS + filtres applicatifs ; R2 privé, URL présignées 15 min ; chiffrement au repos ; purge ; accès Novafrik au contenu uniquement sur consentement tracé                          |
| Enregistrements locaux  | Vol de portable                                                              | AES-256-GCM, clé protégée par DPAPI et liée au compte ; suppression après upload                                                                                                  |
| Comptes                 | Vol de mot de passe, réutilisation de token                                  | Argon2id, JWT 15 min, rotation des refresh tokens avec détection de réutilisation, 2FA, verrouillage progressif                                                                   |
| Paiements               | Faux webhook, rejeu                                                          | Signature + vérification serveur à serveur, idempotence, réconciliation                                                                                                           |
| API                     | Abus, injection, énumération                                                 | Validation Pydantic stricte, requêtes paramétrées, limitation de débit, UUID non séquentiels, CORS restreint                                                                      |
| Fournisseurs IA         | Fuite de données vers des tiers                                              | Contrats de traitement des données (DPA) AssemblyAI / OpenAI, désactivation de la rétention côté fournisseur, aucun entraînement sur nos données, option auto-hébergée en phase 3 |
| Chaîne de build         | Binaire compromis                                                            | CI verrouillée, dépendances épinglées et scannées (cargo audit, pip-audit, npm audit), signature de code, manifeste d'update signé                                                |
| Secrets                 | Exposition                                                                   | Variables d'environnement injectées par le déploiement, jamais dans le dépôt ; rotation trimestrielle ; accès restreint                                                           |

21.2 Conformité réglementaire

| **Cadre**                                                                                                         | **Obligation**                                                                                                               | **Implémentation NovaBrief**                                                                                                                                                                                                                                                                                                                         |
|-------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Cameroun — Loi n° 2024/017 du 23/12/2024 (protection des données), applicable depuis le 23/06/2026, autorité APDP | Base légale et consentement ; registre des traitements ; DPO ; sécurité ; droits des personnes ; notification des violations | Consentement à l'enregistrement : indicateur permanent + mention recommandée en début de réunion (texte fourni dans l'application) ; registre des traitements tenu par Novafrik ; DPO désigné (fondateur formé ou prestataire) ; politique de confidentialité FR / EN ; export et suppression en libre-service ; procédure de notification sous 72 h |
| Cameroun — Loi n° 2010/012 (cybersécurité) et Code pénal                                                          | Enregistrement de conversations avec information des parties                                                                 | Le produit est conçu pour l'organisateur qui informe ses participants ; CGV rappelant l'obligation d'information ; fonction « annonce d'enregistrement » (texte copiable) dans le widget                                                                                                                                                             |
| UE — RGPD (avant ouverture européenne)                                                                            | Base légale, DPA sous-traitants, transferts hors UE, registre, DPIA (données vocales)                                        | Hébergement Hetzner (Allemagne / Finlande), DPA avec les fournisseurs IA, DPIA réalisée avant lancement UE, représentant UE si nécessaire                                                                                                                                                                                                            |
| Bonnes pratiques (ISO 27001 / SOC 2 comme référentiel, sans certification en V1)                                  | Chiffrement, contrôle d'accès, journalisation, sauvegardes, gestion des incidents                                            | TLS 1.3, AES-256 au repos, RLS, audit_log, sauvegardes chiffrées quotidiennes testées mensuellement, runbook d'incident                                                                                                                                                                                                                              |

|                                                                                                                                                                                                                                                                                                     |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **POINT D'ATTENTION —** La loi camerounaise sur les données personnelles est applicable depuis le 23 juin 2026 : la déclaration à l'APDP, le registre et la désignation d'un DPO doivent être faits avant l'ouverture commerciale (M5), pas après. Budget prévu : 400 000 FCFA (juridique one-off). |

22\. Infrastructure, déploiement et exploitation

22.1 Topologie par palier

| **Palier**               | **Capacité**     | **Composition**                                                                                                                   | **Coût mensuel**       |
|--------------------------|------------------|-----------------------------------------------------------------------------------------------------------------------------------|------------------------|
| 1 — Lancement (M1-M18)   | ≤ 2 500 h/mois   | 3 × CAX21 (4 vCPU ARM, 8 Go) : \[api + web + caddy\], \[workers + redis\], \[postgresql\] ; backups Hetzner ; R2 ; DNS Cloudflare | ≈ 32 € (≈ 21 000 FCFA) |
| 2 — Croissance (M18-M36) | ≤ 25 000 h/mois  | 3 × CAX31 (8 vCPU, 16 Go) + load balancer Hetzner + réplica PostgreSQL en lecture + R2                                            | ≈ 95 € (≈ 62 000 FCFA) |
| 3 — Échelle              | \> 25 000 h/mois | Cluster (6-8 CAX41) ou Kubernetes managé, PostgreSQL haute disponibilité, GPU GEX44 × N pour Whisper                              | ≈ 450 € + 234 € / GPU  |

22.2 Environnements et livraison continue

- Trois environnements : dev (Docker Compose local, fournisseurs simulés), staging (copie du palier 1, données synthétiques, fournisseurs en mode test / sandbox Flutterwave), prod.

- GitHub Actions : lint + tests + build à chaque PR ; déploiement staging automatique sur main ; déploiement prod sur tag vX.Y.Z après validation manuelle ; migrations exécutées avant le basculement ; rollback par redéploiement du tag précédent.

- Desktop : build Windows signé sur tag, publication du manifeste d'update sur R2, canal bêta pour les 5 PME pilotes pendant 7 jours avant le canal stable.

- Images Docker multi-arch (ARM64), versions épinglées, scan de vulnérabilités (Trivy) bloquant sur les criticités hautes.

22.3 Sauvegardes, reprise, exploitation

| **Sujet**              | **Règle**                                                                                                                                                                                                 |
|------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Sauvegardes PostgreSQL | pg_dump chiffré toutes les 6 h vers R2 (bucket séparé, autre compte), rétention 30 jours ; snapshot Hetzner quotidien ; restauration testée chaque mois (RPO 6 h, RTO 2 h)                                |
| Audio R2               | Non sauvegardé (éphémère par nature) ; l'original reste sur le poste client 24 h après upload                                                                                                             |
| Monitoring             | Prometheus + Grafana : latence API, longueur de file, durée de traitement, taux d'échec par fournisseur, coût par heure ; Uptime Kuma sur les endpoints publics ; Sentry sur API, workers, web et desktop |
| Alertes                | Taux d'échec de traitement \> 2 % sur 1 h ; file d'attente \> 30 min ; coût par heure \> 200 FCFA ; disjoncteur ouvert ; disque \> 80 % ; échec de sauvegarde                                             |
| Journaux               | Structurés (JSON), corrélés par debug_id, rétention 30 jours, sans contenu de réunion                                                                                                                     |
| Runbooks               | Fournisseur STT indisponible ; file bloquée ; restauration de base ; rotation des secrets ; incident de sécurité ; demande APDP                                                                           |
| Tâches planifiées      | Purge audio (02:00 UTC), relances de renouvellement (08:00 Africa/Douala), réconciliation des paiements, calcul des indicateurs, nettoyage des fichiers temporaires                                       |

23\. Observabilité économique

Le back-office expose le tableau de bord de la section 5.9 alimenté par usage_ledger, payments et subscriptions. Les coûts fournisseurs réels sont rapprochés chaque mois des factures AssemblyAI, OpenAI, Cloudflare et Hetzner ; l'écart entre coût modélisé et coût facturé est un indicateur suivi (objectif \< 5 %). Le modèle Excel joint est mis à jour trimestriellement avec les valeurs observées (utilisation par plan, churn, CAC, coût par heure).

24\. Plan de tests et critères d'acceptation

| **Réf.** | **Test**                        | **Procédure**                                                                                                                     | **Critère de succès**                                                                |
|----------|---------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| T-01     | Intégrité audio hors ligne      | Réunion Teams de 45 min ; couper Ethernet et Wi-Fi à la 20e minute ; terminer hors ligne ; rétablir le réseau                     | Fichier complet, upload repris sans intervention, compte rendu généré ; perte \< 5 s |
| T-02     | Synchronisation micro / système | Signal de test (clics à intervalles connus) joué en local et à distance pendant 60 min                                            | Décalage cumulé \< 40 ms                                                             |
| T-03     | Diarisation                     | 3 locuteurs (2 distants sur Meet, 1 local) pendant 30 min en français                                                             | ≥ 3 locuteurs distincts, ≥ 85 % des phrases correctement attribuées                  |
| T-04     | Non-hallucination               | Transcription contenant 10 formulations vagues et 3 décisions fermes                                                              | 3 décisions extraites, 0 élément issu des formulations vagues (sur 5 exécutions)     |
| T-05     | Qualité de transcription        | Jeu de 50 réunions annotées FR / EN accents camerounais                                                                           | WER ≤ 15 % ; noms du lexique reconnus ≥ 90 %                                         |
| T-06     | Fallback fournisseur            | Bloquer l'API AssemblyAI (réponses 503) pendant un traitement                                                                     | Bascule Deepgram automatique, réunion COMPLETED, incident tracé                      |
| T-07     | Isolation multi-tenant          | Tentatives d'accès croisé par API (IDOR) avec un compte d'une autre organisation, et requête SQL directe sans variable de session | 0 fuite ; RLS bloque la requête directe                                              |
| T-08     | Paiement Mobile Money           | Sandbox Flutterwave : paiement réussi, échoué, webhook rejoué, montant altéré                                                     | Crédit unique, échec géré, rejeu ignoré, montant altéré → REVIEW                     |
| T-09     | Quota et dépassement            | Organisation à 100 % de quota, nouvelle réunion, achat de pack                                                                    | QUOTA_HOLD puis traitement automatique après paiement ; décrément à la seconde       |
| T-10     | Cycle de renouvellement         | Simulation d'horloge : J-3, J-1, J+4, J+30                                                                                        | Relances envoyées, hold à J+4, rétrogradation à J+30, aucune perte de données        |
| T-11     | Charge                          | 200 réunions d'1 h soumises en 10 min sur le palier 1                                                                             | P95 du délai ≤ 8 min, 0 échec non récupéré                                           |
| T-12     | Installation et SmartScreen     | Installation sur Windows 10 et 11 vierges, compte sans droits admin                                                               | Aucun avertissement, \< 30 s, démarrage avec Windows fonctionnel                     |
| T-13     | Périphériques                   | Casque Bluetooth (A2DP puis HFP), USB, changement en cours d'enregistrement                                                       | Aucune coupure \> 500 ms ; avertissement HFP affiché                                 |
| T-14     | Sécurité applicative            | Scan OWASP ZAP, revue des dépendances, test d'expiration et de rotation des tokens                                                | 0 vulnérabilité haute ou critique                                                    |
| T-15     | Accessibilité et langues        | Parcours complet en FR et EN, navigation clavier, lecteur d'écran sur W-04                                                        | Lighthouse accessibilité ≥ 90, aucune chaîne non traduite                            |
| T-16     | Restauration                    | Restauration de la sauvegarde de la veille sur staging                                                                            | Base opérationnelle en \< 2 h, données cohérentes                                    |

24.1 Critère d'acceptation global du MVP

Le MVP est accepté lorsqu'un utilisateur non accompagné, sur un PC Windows standard, peut installer NovaBrief, rejoindre une réunion Teams ou Meet, l'enregistrer, obtenir un compte rendu en moins de 5 minutes par heure de réunion, et qu'une personne absente de la réunion comprend en lisant ce compte rendu ce qui a été décidé et qui doit faire quoi. Les 16 tests ci-dessus sont passés, le NovaBrief Score sur le jeu d'évaluation est ≥ 85 % et le taux d'hallucination est nul.

25\. Roadmap d'exécution

25.1 Phases et portes de décision

| **Phase**              | **Période**                  | **Contenu**                                                                                                                                                                                            | **Porte de sortie (Go / No-Go)**                                                                                  |
|------------------------|------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| 0 — POC                | M1-M2 (oct.-nov. 2026)       | POC \#1 capture WASAPI (micro + loopback, drift, Opus) ; POC \#2 benchmark AssemblyAI vs Deepgram sur 30 réunions FR/EN ; POC \#3 extraction structurée sur 30 transcriptions ; enquête terrain 50 PME | Décalage \< 40 ms ; WER ≤ 15 % ; hallucination = 0 ; ≥ 20 PME déclarent une intention d'achat à 5 000-10 000 FCFA |
| 1 — MVP                | M2-M4 (nov. 2026-janv. 2027) | Desktop, API, pipeline, web, facturation Flutterwave, back-office, tests T-01 à T-16, déclaration APDP, CGV                                                                                            | Tests passés ; 5 PME pilotes installées                                                                           |
| 2 — Bêta privée        | M4 (janvier 2027)            | 5 PME Douala / Yaoundé, 2 réunions / semaine chacune, interviews hebdomadaires, constitution du jeu d'évaluation, corrections                                                                          | NovaBrief Score ≥ 85 % ; ≥ 4 PME prêtes à payer                                                                   |
| 3 — Lancement Cameroun | M5-M12 (févr.-sept. 2027)    | Ouverture commerciale, programme partenaires, contenu, mesure du CAC et du churn, V1.1 (recherche, partage, Stripe en préparation)                                                                     | ≥ 90 clients payants à M12 ; churn ≤ 5 % ; CAC ≤ 10 000 FCFA                                                      |
| 4 — Expansion          | M13-M36                      | Second pays (XOF, PSP local), négociation volume STT, benchmark Whisper, V1.x (recherche sémantique, intégrations, mobile consultation)                                                                | Marge brute ≥ 60 % ; LTV/CAC ≥ 3 ; décision d'auto-hébergement documentée                                         |

25.2 Chronogramme des 16 premières semaines

> Semaine : 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16
>
> POC audio ██ ██
>
> POC STT/LLM ██ ██
>
> Enquête PME ██ ██ ██
>
> Desktop ██ ██ ██ ██ ██
>
> Backend/API ██ ██ ██ ██
>
> Pipeline IA ██ ██ ██ ██
>
> Web app ██ ██ ██ ██
>
> Facturation ██ ██ ██
>
> Back-office ██ ██
>
> Juridique/APDP ██ ██ ██
>
> Recette T-01..16 ██ ██
>
> Bêta privée ██ ██ ██ ██
>
> Lancement ██

25.3 Définition de « terminé »

Une fonctionnalité est terminée lorsqu'elle est couverte par des tests automatisés, documentée dans la référence API ou le guide utilisateur, traduite en FR et EN, journalisée avec le debug_id, déployée en staging et validée par un scénario de recette. Une version est publiable lorsque la CI est verte, que les 16 tests de recette applicables passent et que la note de version est rédigée.

26\. Risques et mitigations

| **Risque**                                                     | **Probabilité** | **Impact** | **Mitigation**                                                                                                        | **Indicateur d'alerte**                    |
|----------------------------------------------------------------|-----------------|------------|-----------------------------------------------------------------------------------------------------------------------|--------------------------------------------|
| Capture audio non fiable (drift, périphériques, loopback muet) | Moyenne         | Critique   | POC \#1 avant tout ; matrice de périphériques en recette ; test audio guidé ; pistes séparées ; télémétrie des échecs | Taux de réunions sans audio système \> 3 % |
| Hallucinations ou tâches mal attribuées                        | Moyenne         | Critique   | Prompt strict, schéma validé, seuils de confiance, jeu d'évaluation, feedback utilisateur                             | NovaBrief Score \< 85 %                    |
| Qualité STT sur accents camerounais                            | Moyenne         | Élevé      | Benchmark POC \#2, lexique métier (keyterms), fournisseur alternatif, option Whisper affiné en phase 3                | WER \> 15 % sur le jeu de test             |
| Churn involontaire Mobile Money                                | Élevée          | Élevé      | Relances multi-canal, période de grâce, paiement en un clic, numéro MoMo enregistré, offre annuelle à −15 %           | Renouvellement à J+3 \< 80 %               |
| CAC supérieur aux hypothèses                                   | Moyenne         | Élevé      | Canal partenaires prioritaire, mesure dès M7, pas de publicité payante avant CAC organique connu                      | CAC \> 12 000 FCFA                         |
| Indisponibilité d'un fournisseur d'IA                          | Faible          | Élevé      | Disjoncteur et fallback automatique, file d'attente persistante                                                       | Disjoncteur ouvert \> 30 min               |
| Hausse des tarifs API                                          | Moyenne         | Moyen      | Abstraction fournisseur, benchmark annuel, seuil d'auto-hébergement documenté                                         | Coût par heure \> 200 FCFA                 |
| Non-conformité APDP / RGPD                                     | Faible          | Élevé      | Déclaration, registre, DPO avant M5 ; DPIA avant l'Europe                                                             | Absence de déclaration à M4                |
| Concurrent international avec prix local                       | Faible          | Moyen      | Différenciation locale (MoMo, offline, partenaires), vitesse d'exécution, mémoire d'entreprise en V1.x                | Perte de deals sur le prix                 |
| Dépendance à un seul développeur (agent IA + CTO)              | Élevée          | Moyen      | Documentation d'architecture, tests, CI, runbooks ; toute connaissance dans le dépôt, jamais dans une tête            | Bus factor = 1 à M12                       |

27\. Gouvernance et pilotage

- **Rituels** : revue hebdomadaire produit (30 min : indicateurs, retours bêta, priorités de la semaine) ; revue mensuelle économique (unit economics, cohortes, décisions de prix) ; rétrospective à chaque porte de phase.

- **Propriété** : le CTO est propriétaire de l'architecture et de la qualité ; le cofondateur commercial est propriétaire du CAC, du churn et des partenariats ; les décisions de prix sont prises à deux sur la base du modèle Excel mis à jour.

- **Documentation vivante** : ce cahier des charges est versionné (v1.0) ; toute décision qui le contredit donne lieu à un ADR et à une mise à jour de version.

- **Indicateurs de pilotage** : activation, réunions par organisation, NovaBrief Score, churn (volontaire / involontaire), CAC, marge brute, coût par heure, LTV/CAC, trésorerie.

**ANNEXES**

Annexe A — Glossaire

| **Terme**        | **Définition**                                                                                                                        |
|------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| WASAPI           | Windows Audio Session API : interface bas niveau d'accès aux flux audio de Windows, utilisée pour la capture du micro et le loopback. |
| Loopback         | Capture du flux de rendu (ce qui sort des haut-parleurs ou du casque), permettant d'enregistrer les participants distants sans bot.   |
| Diarisation      | Segmentation d'un audio par locuteur (« qui parle quand »).                                                                           |
| WER              | Word Error Rate : (substitutions + suppressions + insertions) / mots de référence. Mesure de la qualité de transcription.             |
| Dérive d'horloge | Décalage progressif entre deux flux audio capturés sur des horloges différentes ; doit être compensé pour aligner micro et système.   |
| Opus             | Codec audio ouvert (RFC 6716), optimal pour la voix à faible débit.                                                                   |
| Breakage         | Part des heures payées non consommées à la fin du cycle ; marge pure.                                                                 |
| RLS              | Row-Level Security : filtrage des lignes par PostgreSQL selon le tenant de la session.                                                |
| MRR / ARPU       | Revenu mensuel récurrent / revenu moyen par organisation.                                                                             |
| CAC / LTV        | Coût d'acquisition d'un client / valeur (marge brute) générée par un client sur sa durée de vie.                                      |
| NovaBrief Score  | Produit du taux de tâches validées et du taux de décisions validées par les utilisateurs ; objectif ≥ 85 %.                           |
| APDP             | Autorité de Protection des Données à caractère Personnel (Cameroun, loi n° 2024/017).                                                 |
| Debug_id         | Identifiant unique de diagnostic attaché à chaque réunion de bout en bout.                                                            |
| Circuit breaker  | Disjoncteur logiciel qui isole un fournisseur défaillant et bascule sur l'alternative.                                                |

Annexe B — Hypothèses du modèle financier

Toutes les hypothèses sont modifiables dans la feuille « Hypotheses » du classeur NovaBrief_Modele_Financier_v1.xlsx. Les principales sont rappelées ici avec leur source.

| **Hypothèse**                              | **Valeur**                                     | **Source / justification**                                    |
|--------------------------------------------|------------------------------------------------|---------------------------------------------------------------|
| Taux USD → FCFA                            | 565                                            | exchange-rates.org, 04/09/2026 (564,75)                       |
| Transcription AssemblyAI Universal-3.5 Pro | 0,21 \$/h + 0,02 \$/h diarisation              | assemblyai.com, 15/07/2026                                    |
| Deepgram Nova-3 (fallback)                 | ≈ 0,26 \$/h batch ; 0,0077 \$/min PAYG         | AssemblyAI ; brasstranscripts.com, 14/01/2026                 |
| GPT-5 mini                                 | 0,25 \$ / 2 \$ par million de tokens           | benchlm.ai, 03/09/2026                                        |
| Cloudflare R2                              | 0,015 \$/Go-mois, egress 0                     | egresscost.com, 07/2026                                       |
| Hetzner CAX21 / CAX31 / GEX44              | 7,99 € / 15,99 € / 184 € par mois HT           | bitdoze.com, 12/06/2026 ; hetzner.com                         |
| Flutterwave Cameroun                       | 2 % MoMo, 4,8 % cartes, 1 500 XAF par virement | flutterwave.com/cm/pricing                                    |
| Stripe (EEE)                               | 1,5 % + 0,25 €                                 | checkoutpage.com, 21/04/2026                                  |
| TVA / IS / minimum de perception           | 19,25 % / 33 % / 2,2 % du CA                   | CGI Cameroun (à confirmer LF 2026)                            |
| Utilisation du quota                       | 70 %                                           | Hypothèse Novafrik, sensibilité 50-100 %                      |
| Mix Starter / Team / Business à maturité   | 45 / 40 / 15 %                                 | Hypothèse Novafrik                                            |
| Comptes gratuits actifs par client payant  | 2, consommant 1 h/mois                         | Hypothèse Novafrik                                            |
| Churn mensuel                              | 4 %                                            | Hypothèse Novafrik (MoMo sans prélèvement), sensibilité 2-8 % |
| CAC                                        | 8 000 FCFA                                     | Hypothèse Novafrik, sensibilité 4 000-16 000                  |
| Lancement commercial                       | M5 = février 2027                              | Roadmap section 25                                            |
| Rémunération des fondateurs                | 0 (paramètre)                                  | À renseigner pour un P&L d'entreprise                         |

Annexe C — Sources

- AssemblyAI, « Speech-to-Text API Pricing », 15/07/2026 — assemblyai.com/blog/speech-to-text-api-pricing

- BrassTranscripts, « Deepgram Pricing 2026 », mis à jour 14/01/2026 — brasstranscripts.com

- BenchLM, « OpenAI API Pricing (September 2026) », 03/09/2026 — benchlm.ai/openai/api-pricing

- EgressCost, « Cloudflare R2 Pricing 2026 », 07/2026 — egresscost.com/cloudflare

- Bitdoze, « Hetzner Cloud Pricing After the April 2026 Increase », 12/06/2026 — bitdoze.com

- Hetzner, fiche GEX44 — hetzner.com/dedicated-rootserver/gex44

- Flutterwave, « Tarifs et frais — Cameroun » — flutterwave.com/cm/pricing

- Checkout Page, « Stripe international fees 2026 », 21/04/2026 — checkoutpage.com

- Flexprice, « Resend Pricing in 2026 », 20/08/2026 — flexprice.io

- exchange-rates.org, historique USD/XAF 2026, 04/09/2026

- CIO Mag, « Protection des données au Cameroun : la course contre la montre avant juin 2026 », 23/12/2025

- Fisca Finance, « Les différents régimes d'imposition au Cameroun » (2021) ; CGI 2026

- News du Camer, « Plus de 472 000 PME structurent l'économie locale en 2025 », 25/06/2026

- Investir au Cameroun, « 209 482 entreprises recensées » (RGE2), 27/12/2019

- Granola, « Meeting note tool pricing: Granola vs Fireflies vs Fathom vs Otter », 20/03/2026
