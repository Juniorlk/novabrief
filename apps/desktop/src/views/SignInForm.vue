<script setup lang="ts">
/**
 * EF-11, the first screen anybody sees.
 *
 * The password never leaves this component: it goes to a Tauri command and is
 * not kept, not logged and not put in a store. What comes back is who is
 * signed in, and the token stays on the Rust side.
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";

import { api, type Identified } from "../api";

const emit = defineEmits<{ "signed-in": [who: Identified] }>();

const { t } = useI18n();
const email = ref("");
const password = ref("");
const busy = ref(false);
const failure = ref<string | null>(null);

async function submit(): Promise<void> {
  busy.value = true;
  failure.value = null;
  try {
    emit("signed-in", await api.signIn(email.value, password.value));
  } catch (error) {
    // The message comes from Rust and is already a sentence a person can act
    // on - "the email address or password is not correct" rather than a status
    // code. It never quotes what was submitted.
    failure.value = String(error);
  } finally {
    // Cleared whatever happened: a password left in a field is a password
    // still in memory, and this window can sit open for a whole meeting.
    password.value = "";
    busy.value = false;
  }
}
</script>

<template>
  <form class="sign-in" @submit.prevent="submit">
    <h1>{{ t("signIn.title") }}</h1>
    <p class="lead">{{ t("signIn.lead") }}</p>

    <label for="email">{{ t("signIn.email") }}</label>
    <input
      id="email"
      v-model="email"
      type="email"
      autocomplete="username"
      required
      :disabled="busy"
    />

    <label for="password">{{ t("signIn.password") }}</label>
    <input
      id="password"
      v-model="password"
      type="password"
      autocomplete="current-password"
      required
      :disabled="busy"
    />

    <p v-if="failure" class="failure" role="alert">{{ failure }}</p>

    <button type="submit" :disabled="busy">
      {{ busy ? t("signIn.working") : t("signIn.submit") }}
    </button>

    <p class="note">{{ t("signIn.once") }}</p>
  </form>
</template>

<style scoped>
.sign-in {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

h1 {
  margin: 0;
  font-size: 1.25rem;
}

.lead,
.note {
  margin: 0;
  color: var(--nb-muted);
  font-size: 0.85rem;
}

.note {
  margin-top: 0.5rem;
}

label {
  margin-top: 0.5rem;
  font-size: 0.85rem;
  font-weight: 600;
}

input {
  padding: 0.5rem;
  border: 1px solid var(--nb-border);
  border-radius: 4px;
  background: var(--nb-field);
  color: inherit;
  font: inherit;
}

button {
  margin-top: 1rem;
  padding: 0.6rem;
  border: 0;
  border-radius: 4px;
  background: var(--nb-accent);
  color: #fff;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}

button:disabled {
  opacity: 0.6;
  cursor: default;
}

.failure {
  margin: 0.5rem 0 0;
  color: var(--nb-danger);
  font-size: 0.85rem;
}
</style>
