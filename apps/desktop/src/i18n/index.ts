import { createI18n } from "vue-i18n";

import { en } from "./en";
import { fr } from "./fr";

/** The locales NovaBrief ships. Both are complete from the first screen. */
export const LOCALES = ["fr", "en"] as const;
export type Locale = (typeof LOCALES)[number];

export const messages = { fr, en };

/**
 * French, unless Windows says otherwise.
 *
 * The customer is a Cameroonian SME, so French is the default rather than the
 * fallback — but a machine set to English gets English without anybody having
 * to find a setting. Anything else falls back to French rather than to a key
 * name: a screen showing `status.recording` is worse than one in the wrong
 * language.
 */
export function preferredLocale(candidates: readonly string[]): Locale {
  for (const candidate of candidates) {
    const base = candidate.toLowerCase().split("-")[0];
    if (base && (LOCALES as readonly string[]).includes(base)) {
      return base as Locale;
    }
  }
  return "fr";
}

export function createAppI18n(locale: Locale = preferredLocale(navigator.languages ?? [])) {
  return createI18n({
    legacy: false,
    locale,
    fallbackLocale: "fr",
    messages,
    // A missing key is a bug, and in production it must not reach the user as a
    // dotted path. The warning fires in development; the fallback covers the
    // shipped build.
    missingWarn: import.meta.env.DEV,
    fallbackWarn: import.meta.env.DEV,
  });
}
