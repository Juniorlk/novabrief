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
    // The desktop and web front ends run in a browser engine - WebView2 for
    // the desktop - so `document`, `window` and `navigator` exist. Declared
    // rather than switched off: `no-undef` is what catches a typo in a global,
    // and disabling it everywhere to silence three lines would cost that.
    files: ["apps/desktop/src/**/*.{ts,vue}", "apps/web/**/*.{ts,vue}"],
    languageOptions: {
      globals: {
        document: "readonly",
        navigator: "readonly",
        window: "readonly",
        localStorage: "readonly",
        HTMLSelectElement: "readonly",
        HTMLInputElement: "readonly",
      },
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
      // Formatting belongs to Prettier, and only to Prettier. These two rules
      // rewrite templates in a way Prettier then rewrites back, so `lint:fix`
      // and `format` undo each other and the build never settles.
      "vue/max-attributes-per-line": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/html-closing-bracket-newline": "off",
      "vue/html-indent": "off",
    },
  },
);
