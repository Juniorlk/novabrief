import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// The desktop UI is served to a WebView2 control, not to a browser on the open
// internet, so the usual concerns invert: there is no CDN to rely on and no
// second origin to talk to. Everything ships inside the installer, which is
// also what keeps EF-10's 15 MB budget reachable.
export default defineConfig({
  plugins: [vue()],
  // Tauri serves the built files from a custom protocol at the root.
  base: "./",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Chrome 120 is the floor WebView2 ships on a patched Windows 10, so there
    // is no reason to down-level further and pay for polyfills nobody runs.
    target: "chrome120",
    sourcemap: true,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.ts"],
  },
});
