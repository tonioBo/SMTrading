/// <reference types="vitest" />
import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { configDefaults } from "vitest/config";

const API = process.env.SMT_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  // the build id the API reports in X-SMT-Build (set by the web Dockerfile); "" = no check
  define: { "import.meta.env.VITE_BUILD_ID": JSON.stringify(process.env.VITE_BUILD_ID ?? "") },
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API, changeOrigin: false },
      "/ws": { target: API.replace(/^http/, "ws"), ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    exclude: [...configDefaults.exclude, "e2e/**"],
    css: false,
  },
});
