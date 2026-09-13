/**
 * The three things the window must not get wrong.
 *
 * Not "does it render" - Vue renders. These are the places where a plausible
 * screen would misinform somebody: a task attributed to a person nobody named,
 * a dead audio channel drawn as healthy, and a meeting still sitting on this
 * laptop shown among the ones the server has.
 */
import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import { createI18n } from "vue-i18n";

import { messages } from "../i18n";
import type { MeetingDetail, Snapshot } from "../api";
import MeetingsList from "./MeetingsList.vue";
import RecorderPanel from "./RecorderPanel.vue";
import ReportPanel from "./ReportPanel.vue";

/** Real strings, not stubs: a key that does not exist must fail here too. */
function i18n(locale: "fr" | "en" = "fr") {
  return createI18n({ legacy: false, locale, fallbackLocale: "fr", messages });
}

function detail(overrides: Partial<MeetingDetail["report"]> = {}): MeetingDetail {
  return {
    meeting: {
      id: "m-1",
      debug_id: "DBG-1",
      status: "COMPLETED",
      title: "Point hebdo",
      failed_reason: null,
      duration_seconds: 1800,
    },
    report: {
      title: "Point hebdomadaire",
      participants: ["Awa", "Ibrahim"],
      summary: ["Le budget est arbitré."],
      decisions: [{ content: "Le budget passe à 3 M FCFA.", source_start_ms: 61_000 }],
      tasks: [
        {
          action: "Envoyer le devis",
          assignee_name: null,
          deadline_text: null,
          source_start_ms: 125_000,
        },
      ],
      ...overrides,
    },
    segments: [],
  };
}

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    state: "recording",
    meetingId: "local-1",
    debugId: "DBG-1",
    recordedMs: 30_000,
    discardedMs: 0,
    microphoneLevel: 0.4,
    systemLevel: 0.3,
    warned: false,
    stalled: [],
    failure: null,
    ...overrides,
  };
}

describe("the report", () => {
  /**
   * Section 3: never invent a responsible party. A task the transcript did not
   * attribute must be drawn as unattributed - the name a model guesses is the
   * failure that costs the customer their trust in every other line.
   */
  it("says nobody was named rather than naming somebody", () => {
    const view = mount(ReportPanel, {
      props: { detail: detail() },
      global: { plugins: [i18n()] },
    });

    expect(view.text()).toContain("Responsable non nommé");
    expect(view.text()).toContain("Envoyer le devis");
  });

  /**
   * Every extracted item carries the moment it was said, and that is what lets
   * somebody check it against the recording. A report that cannot be checked
   * is one that has to be trusted.
   */
  it("stamps every decision and task with where it was said", () => {
    const view = mount(ReportPanel, {
      props: { detail: detail() },
      global: { plugins: [i18n()] },
    });

    expect(view.text()).toContain("01:01");
    expect(view.text()).toContain("02:05");
  });

  it("says the report is not ready rather than drawing an empty one", () => {
    const waiting = detail();
    waiting.report = null;
    waiting.meeting.status = "TRANSCRIBING";

    const view = mount(ReportPanel, {
      props: { detail: waiting },
      global: { plugins: [i18n()] },
    });

    expect(view.text()).toContain("pas encore prêt");
  });
});

describe("the meters", () => {
  /**
   * EF-13: a flat system meter must be noticed within five seconds. The panel
   * is what turns "the bar is not moving" into a sentence saying what it costs.
   */
  it("warns after five seconds of silent system audio", async () => {
    vi.useFakeTimers();
    const view = mount(RecorderPanel, {
      props: { snapshot: snapshot({ systemLevel: 0 }) },
      global: { plugins: [i18n()] },
    });

    expect(view.text()).not.toContain("Aucun son système");

    // Four seconds is not yet: a pause between two sentences must not raise it.
    vi.advanceTimersByTime(4_000);
    await view.vm.$nextTick();
    expect(view.text()).not.toContain("Aucun son système");

    vi.advanceTimersByTime(2_000);
    await view.vm.$nextTick();
    expect(view.text()).toContain("Aucun son système");
    vi.useRealTimers();
  });

  /** A live channel is never accused. */
  it("stays quiet while system audio is arriving", async () => {
    vi.useFakeTimers();
    const view = mount(RecorderPanel, {
      props: { snapshot: snapshot({ systemLevel: 0.2 }) },
      global: { plugins: [i18n()] },
    });

    vi.advanceTimersByTime(10_000);
    await view.vm.$nextTick();

    expect(view.text()).not.toContain("Aucun son système");
    vi.useRealTimers();
  });

  /** EF-33: the pause is visible, and said to be unbilled. */
  it("shows what the pauses dropped", () => {
    const view = mount(RecorderPanel, {
      props: { snapshot: snapshot({ discardedMs: 65_000 }) },
      global: { plugins: [i18n()] },
    });

    expect(view.text()).toContain("01:05");
    expect(view.text()).toContain("non facturées");
  });
});

describe("the meeting list", () => {
  /**
   * A recording still on this laptop is not a meeting the server has. Merging
   * the two lists would either hide the ones nobody has sent, or draw them as
   * though they had been - and the second is how a person closes a laptop on a
   * meeting that was never uploaded.
   */
  it("keeps what this machine still owes apart from what the server has", () => {
    const view = mount(MeetingsList, {
      props: {
        meetings: [
          {
            id: "m-1",
            debug_id: "DBG-1",
            status: "COMPLETED",
            title: "Point hebdo",
            failed_reason: null,
            duration_seconds: 600,
          },
        ],
        pending: [
          {
            meetingId: "local-1",
            recordedMs: 120_000,
            partsDone: 1,
            partsTotal: 3,
            attempts: 2,
            lastError: "the NovaBrief service could not be reached",
            abandoned: false,
          },
        ],
        failure: null,
      },
      global: { plugins: [i18n()] },
    });

    expect(view.text()).toContain("Sur ce poste");
    expect(view.text()).toContain("1/3");
    expect(view.text()).toContain("could not be reached");
    expect(view.text()).toContain("Point hebdo");
  });

  /** A meeting still being transcribed cannot be opened, and says so. */
  it("only opens a meeting whose report exists", () => {
    const view = mount(MeetingsList, {
      props: {
        meetings: [
          {
            id: "m-2",
            debug_id: "DBG-2",
            status: "TRANSCRIBING",
            title: null,
            failed_reason: null,
            duration_seconds: 600,
          },
        ],
        pending: [],
        failure: null,
      },
      global: { plugins: [i18n()] },
    });

    expect(view.get("button.open").attributes("disabled")).toBeDefined();
    expect(view.text()).toContain("Transcription");
  });
});
