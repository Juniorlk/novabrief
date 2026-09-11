import { describe, expect, it } from "vitest";

import { en } from "./en";
import { fr } from "./fr";
import { preferredLocale } from "./index";

/** Every leaf key, as a dotted path. */
function paths(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) {
    return [prefix];
  }
  return Object.entries(value).flatMap(([key, child]) =>
    paths(child, prefix ? `${prefix}.${key}` : key),
  );
}

describe("translations", () => {
  /**
   * CLAUDE.md section 6 asks for FR and EN from the first screen. The way that
   * promise breaks is never a missing file — it is one key added to one locale
   * during a hurried change, which then renders as a dotted path in front of a
   * customer. Comparing the key sets is the only check that catches it.
   */
  it("carry exactly the same keys in both locales", () => {
    const french = paths(fr).sort();
    const english = paths(en).sort();

    expect(english.filter((key) => !french.includes(key))).toEqual([]);
    expect(french.filter((key) => !english.includes(key))).toEqual([]);
  });

  it("leave no string empty", () => {
    for (const [locale, bundle] of [
      ["fr", fr],
      ["en", en],
    ] as const) {
      const empty = paths(bundle).filter((path) => {
        const value = path
          .split(".")
          .reduce<unknown>((node, key) => (node as Record<string, unknown>)[key], bundle);
        return typeof value !== "string" || value.trim() === "";
      });
      expect(empty, `${locale} has empty strings`).toEqual([]);
    }
  });
});

describe("preferredLocale", () => {
  it("follows Windows when it asks for a locale we ship", () => {
    expect(preferredLocale(["en-GB", "fr-FR"])).toBe("en");
    expect(preferredLocale(["fr-CM"])).toBe("fr");
  });

  it("falls back to French rather than to nothing", () => {
    // The customer is a Cameroonian SME: French is the default, not the
    // last resort after English.
    expect(preferredLocale(["de-DE", "es-ES"])).toBe("fr");
    expect(preferredLocale([])).toBe("fr");
  });

  it("ignores a region it does not know", () => {
    expect(preferredLocale(["fr-QC"])).toBe("fr");
  });
});
