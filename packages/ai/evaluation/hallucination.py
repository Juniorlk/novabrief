"""The non-hallucination evaluation set (section 18.6, EF-42).

Ten phrasings that a meeting produces constantly and that must yield **nothing
at all**: an intention, a hypothesis, an unfinished debate, a conditional. Risk
number 2 of the specification — "the AI invents a decision" — is measured
here and nowhere else.

The cases are written and runnable now. They are *not* run against a real model
in lot L2: Novafrik cancelled the validation campaign on 2026-09-08, so what
ships is the harness, and the number it produces is unknown until somebody
points it at OpenAI. That is a deliberate deferral, and it stays visible.

Run it with any `LLMProvider`:

    python -m ai.evaluation.hallucination
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ai.extraction import MeetingReport
from ai.llm.base import LLMError, LLMProvider


@dataclass(frozen=True)
class Case:
    """One transcript that must produce no decision and no task."""

    name: str
    transcript: str
    why: str


# Deliberately mundane. A meeting is mostly this, and a model that treats any
# of it as a commitment will fill a report with things nobody agreed to.
VAGUE_CASES: list[Case] = [
    Case(
        name="intention",
        transcript="[0] A: Il faudrait qu'on ameliore notre communication interne.",
        why="an intention with no owner and no commitment",
    ),
    Case(
        name="unfinished_debate",
        transcript=(
            "[0] A: On pourrait regarder d'autres devis.\n[3000] B: Peut-etre, oui. On verra."
        ),
        why="a debate that reached no conclusion",
    ),
    Case(
        name="hypothetical",
        transcript="[0] A: Si le budget passe, on pourrait recruter un commercial.",
        why="a condition that has not been met",
    ),
    Case(
        name="past_report",
        transcript="[0] A: La semaine derniere, on avait valide le principe.",
        why="reporting an old decision is not taking a new one",
    ),
    Case(
        name="question",
        transcript="[0] A: Est-ce que quelqu'un peut regarder le dossier CNPS ?",
        why="an unanswered question assigns nothing",
    ),
    Case(
        name="third_party",
        transcript="[0] A: Le prestataire a dit qu'il livrerait vendredi.",
        why="somebody else's commitment is not our task",
    ),
    Case(
        name="disagreement",
        transcript=(
            "[0] A: On part sur le fournisseur A.\n"
            "[2000] B: Non, attends, je ne suis pas d'accord.\n"
            "[5000] A: Bon, on en reparle."
        ),
        why="a proposal that was contradicted is not a decision",
    ),
    Case(
        name="brainstorm",
        transcript="[0] A: On pourrait faire un webinaire, ou un salon, ou les deux.",
        why="options listed are not options chosen",
    ),
    Case(
        name="small_talk",
        transcript="[0] A: Il fait vraiment chaud a Douala en ce moment.",
        why="there is nothing in it",
    ),
    Case(
        name="rhetorical_commitment",
        transcript="[0] A: On devrait tous faire plus d'efforts sur les delais.",
        why="an exhortation to everyone commits no one",
    ),
]


@dataclass
class Outcome:
    """What the model did with one case."""

    case: Case
    decisions: int
    tasks: int
    error: str | None = None

    @property
    def clean(self) -> bool:
        return self.error is None and self.decisions == 0 and self.tasks == 0


async def run(
    llm: LLMProvider, *, system_prompt: str, cases: list[Case] | None = None
) -> list[Outcome]:
    """Run every case and report what came back."""
    outcomes: list[Outcome] = []
    for case in cases or VAGUE_CASES:
        try:
            report, _ = await llm.extract(system_prompt, case.transcript, MeetingReport)
        except LLMError as exc:
            outcomes.append(Outcome(case=case, decisions=0, tasks=0, error=type(exc).__name__))
            continue
        outcomes.append(
            Outcome(case=case, decisions=len(report.decisions), tasks=len(report.tasks))
        )
    return outcomes


def hallucination_rate(outcomes: list[Outcome]) -> float:
    """Share of cases that produced something. The target is zero."""
    if not outcomes:
        return 0.0
    invented = sum(1 for outcome in outcomes if not outcome.clean)
    return invented / len(outcomes)


def report_lines(outcomes: list[Outcome]) -> list[str]:
    """A human-readable summary."""
    lines = []
    for outcome in outcomes:
        if outcome.clean:
            lines.append(f"ok      {outcome.case.name}")
        elif outcome.error:
            lines.append(f"ERROR   {outcome.case.name}: {outcome.error}")
        else:
            lines.append(
                f"INVENT  {outcome.case.name}: "
                f"{outcome.decisions} decision(s), {outcome.tasks} task(s) "
                f"— {outcome.case.why}"
            )
    lines.append("")
    lines.append(f"hallucination rate: {hallucination_rate(outcomes):.0%} (target 0%)")
    return lines


async def _main() -> int:  # pragma: no cover - operational entry point
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "apps" / "api"))

    from app.config import Settings
    from app.prompts import load
    from app.providers import build_llm

    settings = Settings()
    outcomes = await run(build_llm(settings), system_prompt=load(settings.prompt_version)["system"])
    for line in report_lines(outcomes):
        print(line)  # noqa: T201
    return 0 if hallucination_rate(outcomes) == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_main()))
