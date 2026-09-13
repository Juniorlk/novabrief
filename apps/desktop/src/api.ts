/**
 * Everything the window is allowed to ask of the machine.
 *
 * One module, and no `invoke` anywhere else. Two reasons: the command names
 * are a contract with `src-tauri/src/lib.rs` and a typo in one of them fails
 * at runtime in a WebView, where nobody sees it; and this is the boundary the
 * access token deliberately never crosses. The front end has no token, no
 * base URL and no way to reach the API except through here.
 */
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

/** Where a recording is, as the Rust side names it. */
export type AppState = "idle" | "recording" | "paused" | "uploading" | "processing";

/** What the widget draws, read whole so its parts cannot disagree. */
export interface Snapshot {
  state: AppState;
  meetingId: string;
  debugId: string;
  recordedMs: number;
  discardedMs: number;
  /** Peak over the last tenth of a second, 0 to 1. */
  microphoneLevel: number;
  systemLevel: number;
  /** The five-minute warning of EF-34 has been raised. */
  warned: boolean;
  /** Endpoints that stopped delivering, by name. */
  stalled: string[];
  failure: string | null;
}

/** One entry of the notification-area menu, as the Rust side wants it. */
export interface TrayLabel {
  id: string;
  label: string;
  enabled: boolean;
}

/** What the tray offers in a given state. Mirrors `state.rs::tray_menu`. */
export interface MenuItem {
  id: string;
  label_key: string;
  enabled: boolean;
}

/** Who is signed in. Carries no token, on purpose. */
export interface Identified {
  fullName: string;
  email: string | null;
  role: string;
  locale: string;
  emailVerified: boolean;
}

/** One recording still owed to the server. */
export interface Pending {
  meetingId: string;
  recordedMs: number;
  partsDone: number;
  partsTotal: number;
  attempts: number;
  lastError: string | null;
  abandoned: boolean;
}

/** A meeting as the server knows it. */
export interface Meeting {
  id: string;
  debug_id: string;
  status: string;
  title: string | null;
  failed_reason: string | null;
  duration_seconds: number;
}

export interface TranscriptSegment {
  speaker_tag: string;
  speaker_name: string | null;
  start_ms: number;
  text: string;
}

export interface Decision {
  content: string;
  source_start_ms: number;
}

export interface Task {
  action: string;
  /** Null when nobody was named. The model must not guess one. */
  assignee_name: string | null;
  deadline_text: string | null;
  source_start_ms: number;
}

export interface Report {
  title: string;
  participants: string[];
  summary: string[];
  decisions: Decision[];
  tasks: Task[];
}

export interface MeetingDetail {
  meeting: Meeting;
  /** Null until the analysis has run. */
  report: Report | null;
  segments: TranscriptSegment[];
}

export const api = {
  signIn: (email: string, password: string): Promise<Identified> =>
    invoke("sign_in", { email, password }),
  signOut: (): Promise<void> => invoke("sign_out"),
  session: (): Promise<Identified | null> => invoke("session"),
  isLinked: (): Promise<boolean> => invoke("is_linked"),
  restoreSession: (): Promise<Identified> => invoke("restore_session"),

  startRecording: (limitSeconds: number, title: string | null): Promise<Snapshot> =>
    invoke("start_recording", { limitSeconds, title }),
  pause: (): Promise<AppState> => invoke("pause"),
  resume: (): Promise<AppState> => invoke("resume"),
  finish: (): Promise<AppState> => invoke("finish"),
  snapshot: (): Promise<Snapshot | null> => invoke("snapshot"),

  uploads: (): Promise<Pending[]> => invoke("uploads"),
  meetings: (): Promise<Meeting[]> => invoke("meetings"),
  meetingDetail: (meetingId: string): Promise<MeetingDetail> =>
    invoke("meeting_detail", { meetingId }),

  hideWindow: (): Promise<void> => invoke("hide_window"),
  startsWithWindows: (): Promise<boolean> => invoke("starts_with_windows"),
  setStartsWithWindows: (enabled: boolean): Promise<void> =>
    invoke("set_starts_with_windows", { enabled }),
  menu: (): Promise<MenuItem[]> => invoke("menu"),
  setTrayMenu: (items: TrayLabel[]): Promise<void> => invoke("set_tray_menu", { items }),
};

/**
 * Listen for a notification-area entry being chosen.
 *
 * The Rust side does not act on the menu: it emits, and the window does what
 * its own buttons do. A second path into the recorder would be a second place
 * for the rules to drift.
 */
export function onTray(id: string, handler: () => void): Promise<UnlistenFn> {
  return listen(`tray://${id}`, () => handler());
}

/**
 * `hh:mm:ss`, or `mm:ss` under an hour.
 *
 * The timer is read at a glance by somebody who is in a meeting, so it does
 * not show an hour that is always zero.
 */
export function clock(milliseconds: number): string {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const seconds = String(total % 60).padStart(2, "0");
  const minutes = String(Math.floor(total / 60) % 60).padStart(2, "0");
  const hours = Math.floor(total / 3600);
  return hours > 0 ? `${hours}:${minutes}:${seconds}` : `${minutes}:${seconds}`;
}

/**
 * A timestamp inside the recording, for a line of transcript.
 *
 * Every extracted item carries one (section 3, fidelity): it is what lets
 * somebody check a decision against what was actually said, which is the
 * difference between a report they can act on and one they have to trust.
 */
export function timestamp(milliseconds: number): string {
  return clock(milliseconds);
}
