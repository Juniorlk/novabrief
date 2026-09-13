<script setup lang="ts">
/**
 * The window. One of four screens, and no router.
 *
 * A router would be a dependency and a bundle for four screens whose whole
 * navigation is "sign in, record, look at a meeting, come back" - and EF-10
 * puts the installer under 15 MB. What decides the screen is the state of the
 * machine, not a URL: a recording in progress is drawn whatever the person was
 * looking at, because that is what the window is for.
 */
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import {
  api,
  onTray,
  type Identified,
  type Meeting,
  type MeetingDetail,
  type Pending,
  type Snapshot,
} from "./api";
import { LOCALES, type Locale } from "./i18n";
import MeetingsList from "./views/MeetingsList.vue";
import RecorderPanel from "./views/RecorderPanel.vue";
import ReportPanel from "./views/ReportPanel.vue";
import SignInForm from "./views/SignInForm.vue";

const { t, locale } = useI18n();

const who = ref<Identified | null>(null);
const starting = ref(true);
const snapshot = ref<Snapshot | null>(null);
const meetings = ref<Meeting[]>([]);
const pending = ref<Pending[]>([]);
const opened = ref<MeetingDetail | null>(null);
const listFailure = ref<string | null>(null);
const busy = ref(false);
const actionFailure = ref<string | null>(null);
const autostart = ref(false);

/**
 * Four hours, the technical ceiling of EF-34.
 *
 * **Not the plan limit.** The Free plan stops at sixty minutes and that is a
 * quota: it lives in the `plans` table and ADR-09 forbids writing it here. Lot
 * L5 reads it from the API and passes it instead of this.
 */
const CEILING_SECONDS = 4 * 60 * 60;

/**
 * How often the window asks the machine what it is doing.
 *
 * Four times a second. The meters are what this pays for: EF-13 asks somebody
 * to notice a flat system meter within five seconds, and a meter refreshed
 * once a second looks like a stuck one. The Rust side already keeps the peak
 * over each tenth of a second, so nothing is missed between reads.
 */
const FAST = 250;

/** How often the lists are refreshed. The server does not move faster. */
const SLOW = 5000;

let fastTimer = 0;
let slowTimer = 0;
const unlisten: (() => void)[] = [];

async function readSnapshot(): Promise<void> {
  try {
    snapshot.value = await api.snapshot();
  } catch {
    // A snapshot that cannot be read is not worth a message: the next one is
    // 250 ms away, and an error banner that flickers is worse than nothing.
    snapshot.value = null;
  }
}

async function readLists(): Promise<void> {
  if (!who.value) {
    return;
  }
  try {
    pending.value = await api.uploads();
  } catch {
    pending.value = [];
  }
  try {
    meetings.value = await api.meetings();
    listFailure.value = null;
  } catch (error) {
    // This one is worth saying: it means the server is unreachable or the
    // session is over, and the second has an answer the person must take.
    listFailure.value = String(error);
  }
}

async function open(meetingId: string): Promise<void> {
  try {
    opened.value = await api.meetingDetail(meetingId);
  } catch (error) {
    listFailure.value = String(error);
  }
}

function signedIn(identified: Identified): void {
  who.value = identified;
  useTheirLanguage(identified.locale);
  void readLists();
}

async function signOut(): Promise<void> {
  await api.signOut();
  who.value = null;
  meetings.value = [];
  opened.value = null;
}

/** Their account says which language they read; the machine only guessed. */
function useTheirLanguage(preferred: string): void {
  const wanted = preferred.toLowerCase().split("-")[0];
  if (wanted && (LOCALES as readonly string[]).includes(wanted)) {
    setLocale(wanted as Locale);
  }
}

/** EF-10: optional, and only when somebody asks for it. */
async function toggleAutostart(enabled: boolean): Promise<void> {
  try {
    await api.setStartsWithWindows(enabled);
    autostart.value = enabled;
  } catch (error) {
    actionFailure.value = String(error);
    // Left showing what the machine actually does, not what was asked.
    autostart.value = await api.startsWithWindows().catch(() => false);
  }
}

function setLocale(next: Locale): void {
  locale.value = next;
  document.documentElement.lang = next;
}

onMounted(async () => {
  document.documentElement.lang = locale.value;
  try {
    // Silent when this machine has never been linked: the sign-in screen is
    // the right answer, not an error about a session that never existed.
    if (await api.isLinked()) {
      signedIn(await api.restoreSession());
    }
  } catch {
    who.value = null;
  } finally {
    starting.value = false;
  }

  fastTimer = window.setInterval(() => void readSnapshot(), FAST);
  slowTimer = window.setInterval(() => void readLists(), SLOW);
  void readSnapshot();
  void refreshTray();
  try {
    autostart.value = await api.startsWithWindows();
  } catch {
    // A machine whose registry refuses to be read still records. The setting
    // simply shows as off, which is what it effectively is.
    autostart.value = false;
  }

  // The same operations the buttons call, reached from the notification area.
  unlisten.push(await onTray("start", () => void start()));
  unlisten.push(
    await onTray("pause", () => {
      // One entry, because that is what the menu shows: it reads Pause or
      // Resume depending on the state, and whoever chooses it means the other
      // one.
      void (snapshot.value?.state === "paused" ? resume() : pause());
    }),
  );
  unlisten.push(await onTray("stop", () => void stop()));
});

onUnmounted(() => {
  window.clearInterval(fastTimer);
  window.clearInterval(slowTimer);
  for (const stopListening of unlisten) {
    stopListening();
  }
});

/**
 * Everything that changes the recording goes through here.
 *
 * The buttons and the notification-area menu offer the same four things, and
 * two callers deciding separately whether a pause is allowed right now is how
 * they come to disagree.
 */
async function act(operation: () => Promise<unknown>): Promise<void> {
  busy.value = true;
  actionFailure.value = null;
  try {
    await operation();
  } catch (error) {
    actionFailure.value = String(error);
  } finally {
    busy.value = false;
  }
  await readSnapshot();
  await readLists();
}

const start = (): Promise<void> => act(() => api.startRecording(CEILING_SECONDS, null));
const pause = (): Promise<void> => act(() => api.pause());
const resume = (): Promise<void> => act(() => api.resume());
const stop = (): Promise<void> => act(() => api.finish());

/**
 * Keep the notification-area menu in step with the state and the language.
 *
 * The labels are translated here because i18n lives here (`CLAUDE.md` section
 * 6); which entries exist and which are enabled is still `state.rs`, so the
 * two cannot disagree about when Pause is allowed.
 */
async function refreshTray(): Promise<void> {
  try {
    const items = await api.menu();
    await api.setTrayMenu(
      items.map((item) => ({ id: item.id, label: t(item.label_key), enabled: item.enabled })),
    );
  } catch {
    // A machine whose notification area refuses a menu still records. The
    // window has the same four buttons.
  }
}

watch([() => snapshot.value?.state, locale], () => void refreshTray());

/** A recording in progress outranks whatever was being read. */
const capturing = computed(
  () => snapshot.value?.state === "recording" || snapshot.value?.state === "paused",
);
</script>

<template>
  <main class="shell">
    <p v-if="starting" class="starting">{{ t("app.starting") }}</p>

    <SignInForm v-else-if="!who" @signed-in="signedIn" />

    <template v-else>
      <header>
        <h1>{{ t("app.name") }}</h1>
        <button type="button" class="link" @click="signOut">{{ t("app.signOut") }}</button>
      </header>

      <RecorderPanel
        :snapshot="snapshot"
        :busy="busy"
        :failure="actionFailure"
        @start="start"
        @pause="pause"
        @resume="resume"
        @stop="stop"
      />

      <ReportPanel v-if="opened && !capturing" :detail="opened" @back="opened = null" />
      <MeetingsList
        v-else-if="!capturing"
        :meetings="meetings"
        :pending="pending"
        :failure="listFailure"
        @open="open"
      />
    </template>

    <footer>
      <label class="autostart">
        <input
          type="checkbox"
          :checked="autostart"
          @change="toggleAutostart(($event.target as HTMLInputElement).checked)"
        />
        {{ t("settings.autostart") }}
      </label>
      <label for="locale">{{ t("language.label") }}</label>
      <select
        id="locale"
        :value="locale"
        @change="setLocale(($event.target as HTMLSelectElement).value as Locale)"
      >
        <option v-for="code in LOCALES" :key="code" :value="code">
          {{ t(`language.${code}`) }}
        </option>
      </select>
      <button type="button" class="link" @click="api.hideWindow()">{{ t("app.hide") }}</button>
    </footer>
  </main>
</template>

<style>
:root {
  --nb-muted: #6b7280;
  --nb-border: #d1d5db;
  --nb-field: #f3f4f6;
  --nb-accent: #2563eb;
  --nb-danger: #dc2626;
  color-scheme: light dark;
}

@media (prefers-color-scheme: dark) {
  :root {
    --nb-muted: #9ca3af;
    --nb-border: #374151;
    --nb-field: #1f2937;
    --nb-accent: #60a5fa;
    --nb-danger: #f87171;
  }
}
</style>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  padding: 1rem;
  min-height: 100vh;
  box-sizing: border-box;
  font-family: "Segoe UI", system-ui, sans-serif;
}

header {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}

h1 {
  margin: 0;
  font-size: 1.1rem;
}

.starting {
  margin: auto;
  color: var(--nb-muted);
}

footer {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: auto;
  padding-top: 0.5rem;
  font-size: 0.8rem;
  color: var(--nb-muted);
}

.autostart {
  display: flex;
  align-items: center;
  gap: 0.3rem;
}

.link {
  margin-left: auto;
  padding: 0;
  border: 0;
  background: none;
  color: var(--nb-accent);
  font: inherit;
  font-size: 0.8rem;
  cursor: pointer;
}

select {
  padding: 0.2rem;
  border: 1px solid var(--nb-border);
  border-radius: 4px;
  background: var(--nb-field);
  color: inherit;
  font: inherit;
  font-size: 0.8rem;
}
</style>
