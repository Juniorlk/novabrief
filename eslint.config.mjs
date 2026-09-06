// Flat ESLint configuration for the whole monorepo.
// Type-aware rules are switched on per workspace once apps/web and
// packages/schemas have their own tsconfig; the root config carries the
// language-agnostic rules and the strict TypeScript baseline.
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import vue from "eslint-plugin-vue";
import vueParser from "vue-eslint-parser";

export default tseslint.config(
  {
    ignores: [
      "**/node_modules/**",
      "**/dist/**",
      "**/.nuxt/**",
      "**/.output/**",
      "**/target/**",
      "**/coverage/**",
      // Local Python environments: their packages vendor JavaScript that is
      // not our code.
      "**/.venv/**",
      "**/venv/**",
      "**/__pycache__/**",
      "tools/datasets/**",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.strict,
  ...tseslint.configs.stylistic,
  {
    files: ["**/*.{ts,mts,cts}"],
    rules: {
      "@typescript-eslint/consistent-type-imports": "error",
      "@typescript-eslint/no-explicit-any": "error",
      "no-console": ["error", { allow: ["warn", "error"] }],
    },
  },
  {
    files: ["**/*.vue"],
    extends: [...vue.configs["flat/recommended"]],
    languageOptions: {
      parser: vueParser,
      parserOptions: { parser: tseslint.parser, extraFileExtensions: [".vue"] },
    },
    rules: {
      "vue/multi-word-component-names": "error",
      // Every user-visible string goes through i18n (CLAUDE.md section 6).
      "vue/no-bare-strings-in-template": "warn",
    },
  },
);
