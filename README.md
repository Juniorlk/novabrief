# NovaBrief

Assistant intelligent de réunions édité par **Novafrik**. NovaBrief enregistre une
réunion depuis un PC Windows (micro + audio système, sans bot, sans dépendre de
Teams / Meet / Zoom) et produit un compte rendu structuré : titre, participants,
résumé, décisions, tâches avec responsable et échéance, transcription horodatée.

- Référence complète : [`docs/cahier-des-charges.md`](docs/cahier-des-charges.md) (NVK-CDC-NB-2026-V1.0)
- Instructions permanentes de développement : [`CLAUDE.md`](CLAUDE.md)
- Décisions d'architecture : [`docs/adr/`](docs/adr/)
- Briefs de tâches : [`docs/tasks/`](docs/tasks/)

## État du projet

**Phase 0 — POC.** Rien du SaaS n'est développé tant que les trois POC ne sont pas
validés par des mesures, dans l'ordre : capture audio Windows, benchmark de
transcription FR/EN, extraction structurée.

| Étape | État |
|---|---|
| Amorçage du dépôt et outillage | fait |
| POC #1 — capture audio Windows ([brief](docs/tasks/01_POC1_capture_audio.md)) | à démarrer |
| POC #2 — benchmark transcription FR/EN | en attente du Go POC #1 |
| POC #3 — extraction structurée | en attente du Go POC #2 |

## Structure

```
apps/desktop   Application Windows Tauri 2 (moteur audio Rust, UI Vue 3)
apps/api       Backend FastAPI + workers Celery
apps/web       Interface web Nuxt 3
packages/schemas  Schémas Pydantic — source de vérité, génèrent les types TS
packages/ai       Providers transcription / LLM, prompts versionnés, évaluation
infra          docker-compose, Caddy, déploiement, sauvegardes
tools          Outillage hors production (nb-capture, nb-testsignal, mesures)
docs           Cahier des charges, ADR, briefs de tâches
```

## Prérequis

Windows 10 (21H2+) ou 11 x64 — le moteur audio se développe et se teste sur
Windows.

| Outil | Version | Installation |
|---|---|---|
| Git | 2.40+ | `winget install Git.Git` |
| Rust | stable (voir `rust-toolchain.toml`) | `winget install Rustlang.Rustup` |
| MSVC Build Tools | 2022, workload « C++ » | voir ci-dessous — **obligatoire**, la cible `x86_64-pc-windows-msvc` ne peut pas éditer les liens sans lui |
| Python | 3.12 | `winget install Python.Python.3.12` |
| Node.js | 20+ | `winget install OpenJS.NodeJS.LTS` |
| pnpm | via corepack | `corepack enable` |
| GNU Make | 4.x | `winget install ezwinports.make` |
| Docker Desktop | — | pour `infra/` (lot backend) |
| GitHub CLI | 2.x | `winget install GitHub.cli`, puis `gh auth login` une fois — sert à lire les résultats de CI |

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools `
  --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

> Sans ces outils, `cargo build` échoue avec `linking with 'link.exe' failed` :
> Git Bash fournit un `link` GNU homonyme qui est ramassé à la place de l'éditeur
> de liens MSVC. Le symptôme est trompeur, la cause est l'absence de MSVC.

## Démarrage

Depuis **Git Bash** :

```bash
make setup     # installe l'outillage des trois langages
make lint      # ruff + mypy --strict, clippy -D warnings, eslint + tsc
make test      # pytest, cargo test, vitest
make fmt       # formate les trois langages
make poc1      # compile et lance nb-capture (Windows uniquement)
```

## Configuration

Copier `.env.example` en `.env` et renseigner les variables. `.env` est ignoré par
Git : **un secret commité est un incident**, il se révoque immédiatement.

## Conventions

Commits conventionnels, PR < 400 lignes de diff, jamais de commit direct sur
`main`, CI verte obligatoire. Le détail est dans [`CLAUDE.md`](CLAUDE.md) §6.

---

Confidentiel — Novafrik.
