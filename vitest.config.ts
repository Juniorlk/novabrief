import { defineConfig } from "vitest/config";

/**
 * One runner, each workspace on its own terms.
 *
 * Without this, `vitest` at the root discovers every test file and compiles
 * them with a bare config - which has no Vue plugin, so a test that mounts a
 * component fails to parse the component rather than failing an assertion.
 * The failure reads like a broken test and is a missing plugin.
 *
 * Projects make each workspace use its own `vite.config.ts`, which is where
 * the plugins and the DOM environment already live.
 */
export default defineConfig({
  test: {
    projects: ["apps/*", "packages/*"],
  },
});
