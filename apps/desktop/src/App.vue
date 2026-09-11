<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

import { LOCALES, type Locale } from "./i18n";

const { t, locale } = useI18n();

/**
 * What the tray icon is showing right now.
 *
 * Held here rather than in the Rust side's head alone: the window is a view
 * onto the recorder, and lot L3.3 will feed this from the state machine. Until
 * then it is idle, and honestly so - no fake animation pretending to record.
 */
const status = ref<"idle" | "recording" | "paused" | "uploading" | "processing">("idle");

const statusLabel = computed(() => t(`status.${status.value}`));

/** Windows decides, unless the person has said otherwise. */
onMounted(() => {
  document.documentElement.lang = locale.value;
});

function setLocale(next: Locale): void {
  locale.value = next;
  document.documentElement.lang = next;
}
</script>

<template>
  <main class="shell">
    <header>
      <h1>{{ t("app.name") }}</h1>
      <p class="tagline">
        {{ t("app.tagline") }}
      </p>
    </header>

    <p class="status" :data-state="status">
      {{ statusLabel }}
    </p>

    <footer>
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
    </footer>
  </main>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  padding: 2rem;
  font-family: "Segoe UI", system-ui, sans-serif;
  color: #111827;
}

h1 {
  margin: 0;
  font-size: 1.5rem;
}

.tagline {
  margin: 0.25rem 0 0;
  color: #6b7280;
}

.status {
  margin: 0;
  font-weight: 600;
}

/* The recording state is the one a person has to recognise without reading. */
.status[data-state="recording"] {
  color: #dc2626;
}

footer {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-top: auto;
}

@media (prefers-color-scheme: dark) {
  .shell {
    color: #f9fafb;
  }

  .tagline {
    color: #9ca3af;
  }
}
</style>
