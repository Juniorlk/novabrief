<script setup lang="ts">
/**
 * What this organization has, and what this machine still owes it.
 *
 * Two lists, deliberately not merged. The server list is the truth about
 * meetings; the queue is the truth about **this laptop**, and a recording
 * waiting here because the network is down does not exist on the server at all
 * yet. Showing one list would either hide the meetings nobody has sent or
 * pretend they had been.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import { clock, type Meeting, type Pending } from "../api";

const props = defineProps<{
  meetings: Meeting[];
  pending: Pending[];
  failure: string | null;
}>();
defineEmits<{ open: [meetingId: string] }>();

const { t } = useI18n();

/** Statuses that mean the server is still working on it. */
const WORKING = new Set(["UPLOADING", "QUEUED", "TRANSCRIBING", "FALLBACK_STT", "ANALYZING"]);

const nothing = computed(() => props.meetings.length === 0 && props.pending.length === 0);

function readable(meeting: Meeting): boolean {
  return meeting.status === "COMPLETED" || meeting.status === "PUBLISHED";
}

function share(entry: Pending): string {
  if (entry.partsTotal === 0) {
    return "";
  }
  return ` ${entry.partsDone}/${entry.partsTotal}`;
}
</script>

<template>
  <section class="meetings">
    <p v-if="failure" class="failure" role="alert">{{ failure }}</p>

    <template v-if="pending.length > 0">
      <h2>{{ t("meetings.onThisMachine") }}</h2>
      <ul class="list">
        <li v-for="entry in pending" :key="entry.meetingId" class="row pending">
          <div class="main">
            <span class="title">{{ clock(entry.recordedMs) }}</span>
            <span class="detail">
              {{ entry.abandoned ? t("meetings.stuck") : t("meetings.sending") }}{{ share(entry) }}
            </span>
          </div>
          <p v-if="entry.lastError" class="why">{{ entry.lastError }}</p>
        </li>
      </ul>
    </template>

    <h2>{{ t("meetings.title") }}</h2>
    <p v-if="nothing" class="empty">{{ t("meetings.empty") }}</p>

    <ul class="list">
      <li v-for="meeting in meetings" :key="meeting.id" class="row">
        <button
          type="button"
          class="open"
          :disabled="!readable(meeting)"
          @click="$emit('open', meeting.id)"
        >
          <span class="title">{{ meeting.title ?? t("meetings.untitled") }}</span>
          <span class="detail">
            {{ clock(meeting.duration_seconds * 1000) }} ·
            <span :data-working="WORKING.has(meeting.status)">
              {{ t(`meetingStatus.${meeting.status}`) }}
            </span>
          </span>
        </button>
        <p v-if="meeting.failed_reason" class="why">
          {{ t(`failures.${meeting.failed_reason}`) }}
        </p>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.meetings {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

h2 {
  margin: 0.5rem 0 0;
  font-size: 0.8rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--nb-muted);
}

.list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.row {
  border: 1px solid var(--nb-border);
  border-radius: 4px;
}

.row.pending {
  padding: 0.5rem;
}

.open {
  display: block;
  width: 100%;
  padding: 0.5rem;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.open:disabled {
  cursor: default;
  opacity: 0.7;
}

.main {
  display: flex;
  flex-direction: column;
}

.title {
  display: block;
  font-weight: 600;
}

.detail {
  display: block;
  font-size: 0.8rem;
  color: var(--nb-muted);
}

.detail [data-working="true"] {
  color: var(--nb-accent);
}

.why,
.empty {
  margin: 0.25rem 0 0;
  font-size: 0.8rem;
  color: var(--nb-muted);
}

.why {
  padding: 0 0.5rem 0.5rem;
}

.failure {
  margin: 0;
  color: var(--nb-danger);
  font-size: 0.85rem;
}
</style>
