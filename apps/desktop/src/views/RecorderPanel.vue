<script setup lang="ts">
/**
 * The widget: EF-12, EF-13, EF-33, EF-34.
 *
 * The two meters are the point, not decoration. EF-13 asks that somebody
 * notice within five seconds that system audio is not being captured, and that
 * only works if the two are drawn apart: one mixed level, on a machine with a
 * live microphone, looks perfectly healthy while the remote voices are not
 * being recorded at all - which is the most expensive failure this product
 * has, because it is discovered when the report arrives half empty.
 */
import { computed, onUnmounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { clock, type Snapshot } from "../api";

const props = defineProps<{ snapshot: Snapshot | null; busy: boolean; failure: string | null }>();
// Asks rather than acts. The notification-area menu offers the same four
// things, and a panel that called the recorder itself would be a second place
// for "may this be paused right now" to be decided.
defineEmits<{ start: []; pause: []; resume: []; stop: [] }>();

const { t } = useI18n();

/**
 * How long the system meter has been flat while recording.
 *
 * EF-13 is a five-second criterion, so the warning appears at five seconds -
 * long enough that a pause between two sentences does not raise it, short
 * enough to be the difference between restarting a meeting and losing it.
 */
const silentSince = ref<number | null>(null);
const now = ref(Date.now());
const tick = window.setInterval(() => {
  now.value = Date.now();
}, 500);
onUnmounted(() => window.clearInterval(tick));

watch(
  () => props.snapshot,
  (snapshot) => {
    if (!snapshot || snapshot.state !== "recording") {
      silentSince.value = null;
      return;
    }
    if (snapshot.systemLevel > 0.001) {
      silentSince.value = null;
    } else if (silentSince.value === null) {
      silentSince.value = Date.now();
    }
  },
  { immediate: true },
);

const systemIsFlat = computed(
  () => silentSince.value !== null && now.value - silentSince.value >= 5000,
);

const recording = computed(() => props.snapshot?.state === "recording");
const paused = computed(() => props.snapshot?.state === "paused");
const capturing = computed(() => recording.value || paused.value);

/** A meter reading as a width, with a floor so "alive but quiet" is visible. */
function bar(level: number): string {
  // Loudness is logarithmic; a linear bar spends most of its length on sounds
  // nobody can hear and barely moves for speech. Cube-rooting it puts normal
  // speech in the middle of the bar, which is where a meter is read.
  const scaled = Math.min(1, Math.cbrt(Math.max(0, level)));
  return `${Math.round(scaled * 100)}%`;
}
</script>

<template>
  <section class="recorder">
    <div class="headline">
      <span class="dot" :data-state="snapshot?.state ?? 'idle'" aria-hidden="true" />
      <span class="state">{{ t(`status.${snapshot?.state ?? "idle"}`) }}</span>
      <span v-if="capturing" class="timer">{{ clock(snapshot?.recordedMs ?? 0) }}</span>
    </div>

    <div v-if="capturing" class="meters">
      <div class="meter">
        <span class="label">{{ t("meters.microphone") }}</span>
        <div class="track">
          <div class="fill" :style="{ width: bar(snapshot?.microphoneLevel ?? 0) }" />
        </div>
      </div>
      <div class="meter">
        <span class="label">{{ t("meters.system") }}</span>
        <div class="track">
          <div
            class="fill"
            :data-flat="systemIsFlat"
            :style="{ width: bar(snapshot?.systemLevel ?? 0) }"
          />
        </div>
      </div>
    </div>

    <p v-if="systemIsFlat" class="warning" role="alert">
      {{ t("meters.systemSilent") }}
    </p>
    <p v-if="snapshot?.warned" class="warning" role="alert">
      {{ t("recorder.nearLimit") }}
    </p>
    <p v-if="(snapshot?.discardedMs ?? 0) > 0" class="note">
      {{ t("recorder.paused", { time: clock(snapshot?.discardedMs ?? 0) }) }}
    </p>
    <p v-if="failure" class="failure" role="alert">{{ failure }}</p>

    <div class="controls">
      <button v-if="!capturing" type="button" :disabled="busy" @click="$emit('start')">
        {{ t("tray.start") }}
      </button>
      <button v-if="recording" type="button" :disabled="busy" @click="$emit('pause')">
        {{ t("tray.pause") }}
      </button>
      <button v-if="paused" type="button" :disabled="busy" @click="$emit('resume')">
        {{ t("tray.resume") }}
      </button>
      <button v-if="capturing" type="button" class="stop" :disabled="busy" @click="$emit('stop')">
        {{ t("tray.stop") }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.recorder {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.headline {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.state {
  font-weight: 600;
}

.timer {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  font-size: 1.1rem;
  font-weight: 600;
}

.dot {
  width: 0.7rem;
  height: 0.7rem;
  border-radius: 50%;
  background: var(--nb-muted);
}

/* The one state a person must recognise without reading it. */
.dot[data-state="recording"] {
  background: var(--nb-danger);
  animation: pulse 1.6s ease-in-out infinite;
}

.dot[data-state="paused"] {
  background: #f59e0b;
}

.dot[data-state="uploading"],
.dot[data-state="processing"] {
  background: var(--nb-accent);
}

@keyframes pulse {
  50% {
    opacity: 0.3;
  }
}

/* A person who has set their system to reduce motion has asked not to be
   shown a blinking light. The colour still says which state it is. */
@media (prefers-reduced-motion: reduce) {
  .dot[data-state="recording"] {
    animation: none;
  }
}

.meters {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.meter {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.label {
  width: 6.5rem;
  font-size: 0.8rem;
  color: var(--nb-muted);
}

.track {
  flex: 1;
  height: 0.5rem;
  border-radius: 999px;
  background: var(--nb-field);
  overflow: hidden;
}

.fill {
  height: 100%;
  background: var(--nb-accent);
  transition: width 0.1s linear;
}

.fill[data-flat="true"] {
  background: var(--nb-danger);
}

.controls {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.25rem;
}

button {
  flex: 1;
  padding: 0.5rem;
  border: 1px solid var(--nb-border);
  border-radius: 4px;
  background: var(--nb-field);
  color: inherit;
  font: inherit;
  cursor: pointer;
}

button.stop {
  border-color: var(--nb-danger);
  color: var(--nb-danger);
  font-weight: 600;
}

button:disabled {
  opacity: 0.6;
  cursor: default;
}

.warning {
  margin: 0;
  color: var(--nb-danger);
  font-size: 0.85rem;
}

.note {
  margin: 0;
  color: var(--nb-muted);
  font-size: 0.8rem;
}

.failure {
  margin: 0;
  color: var(--nb-danger);
  font-size: 0.85rem;
}
</style>
