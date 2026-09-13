<script setup lang="ts">
/**
 * The compte rendu, in the software.
 *
 * Every decision and every task carries the moment it was said, and that is
 * not decoration either: section 3 forbids the analysis from inventing a
 * decision, a task, an owner or a date, and a timestamp is what lets somebody
 * check. A report nobody can check is a report nobody can act on.
 *
 * Where the model found no owner, this draws no owner. "Unassigned" is a
 * true statement about the meeting; a guessed name is the failure the whole
 * product is judged on.
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";

import { timestamp, type MeetingDetail } from "../api";

defineProps<{ detail: MeetingDetail }>();
defineEmits<{ back: [] }>();

const { t } = useI18n();
const showTranscript = ref(false);
</script>

<template>
  <section class="report">
    <button type="button" class="back" @click="$emit('back')">
      {{ t("report.back") }}
    </button>

    <template v-if="detail.report">
      <h1>{{ detail.report.title }}</h1>

      <p v-if="detail.report.participants.length > 0" class="participants">
        {{ detail.report.participants.join(", ") }}
      </p>

      <h2>{{ t("report.summary") }}</h2>
      <ul class="summary">
        <li v-for="(point, index) in detail.report.summary" :key="index">{{ point }}</li>
      </ul>

      <h2>{{ t("report.decisions") }}</h2>
      <p v-if="detail.report.decisions.length === 0" class="none">
        {{ t("report.noDecisions") }}
      </p>
      <ul class="items">
        <li v-for="(decision, index) in detail.report.decisions" :key="index">
          <span class="content">{{ decision.content }}</span>
          <span class="at">{{ timestamp(decision.source_start_ms) }}</span>
        </li>
      </ul>

      <h2>{{ t("report.tasks") }}</h2>
      <p v-if="detail.report.tasks.length === 0" class="none">{{ t("report.noTasks") }}</p>
      <ul class="items">
        <li v-for="(task, index) in detail.report.tasks" :key="index">
          <span class="content">{{ task.action }}</span>
          <span class="who">
            {{ task.assignee_name ?? t("report.nobodyNamed") }}
            <template v-if="task.deadline_text"> · {{ task.deadline_text }}</template>
          </span>
          <span class="at">{{ timestamp(task.source_start_ms) }}</span>
        </li>
      </ul>
    </template>

    <p v-else class="none">{{ t("report.notReady") }}</p>

    <template v-if="detail.segments.length > 0">
      <button type="button" class="toggle" @click="showTranscript = !showTranscript">
        {{ showTranscript ? t("report.hideTranscript") : t("report.showTranscript") }}
      </button>
      <ol v-if="showTranscript" class="transcript">
        <li v-for="(line, index) in detail.segments" :key="index">
          <span class="at">{{ timestamp(line.start_ms) }}</span>
          <span class="speaker">{{ line.speaker_name ?? line.speaker_tag }}</span>
          <span class="said">{{ line.text }}</span>
        </li>
      </ol>
    </template>
  </section>
</template>

<style scoped>
.report {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

h1 {
  margin: 0;
  font-size: 1.15rem;
}

h2 {
  margin: 0.75rem 0 0;
  font-size: 0.8rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--nb-muted);
}

.participants,
.none {
  margin: 0;
  font-size: 0.85rem;
  color: var(--nb-muted);
}

.summary,
.items {
  margin: 0;
  padding-left: 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  font-size: 0.9rem;
}

.items {
  list-style: none;
  padding-left: 0;
}

.items li {
  display: flex;
  flex-direction: column;
  padding: 0.4rem 0.5rem;
  border: 1px solid var(--nb-border);
  border-radius: 4px;
}

.who,
.at {
  font-size: 0.78rem;
  color: var(--nb-muted);
}

.at {
  font-variant-numeric: tabular-nums;
}

.back,
.toggle {
  align-self: flex-start;
  padding: 0.3rem 0.6rem;
  border: 1px solid var(--nb-border);
  border-radius: 4px;
  background: var(--nb-field);
  color: inherit;
  font: inherit;
  font-size: 0.8rem;
  cursor: pointer;
}

.transcript {
  margin: 0;
  padding-left: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  font-size: 0.85rem;
}

.transcript li {
  display: grid;
  grid-template-columns: 3.5rem 6rem 1fr;
  gap: 0.4rem;
}

.speaker {
  font-weight: 600;
}

.said {
  white-space: pre-wrap;
}
</style>
