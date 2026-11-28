/**
 * Build of the «SMTrading TV» Tizen app (tv-app/): web/tv-app.html + src/tvapp into
 * ../tv-app/build/www, as files a packaged app loads from its own folder: relative paths,
 * a single classic script (no ES module: a Tizen app is served from file://), no build-id
 * check (the app does not follow the portal's builds). `npm run build:tv-app`.
 */
import fs from "node:fs";
import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const OUT = path.resolve(__dirname, "../tv-app/build/www");

/** tv-app.html → index.html, and its module script as a classic deferred one */
function tizenHtml(): Plugin {
  return {
    name: "smt-tv-app-html",
    enforce: "post",
    transformIndexHtml: {
      order: "post",
      handler: (html) =>
        html
          .replace(/<script type="module" crossorigin src="([^"]+)"><\/script>/g, '<script defer src="$1"></script>')
          .replace(/<link rel="stylesheet" crossorigin href=/g, '<link rel="stylesheet" href='),
    },
    generateBundle(_, bundle) {
      const page = bundle["tv-app.html"];
      if (page) page.fileName = "index.html";
      // the third-party notices travel with the app (no other file of public/)
      this.emitFile({ type: "asset", fileName: "NOTICE.txt", source: fs.readFileSync(path.resolve(__dirname, "public/NOTICE.txt")) });
    },
  };
}

export default defineConfig({
  plugins: [react(), tizenHtml()],
  base: "./",
  publicDir: false,
  define: { "import.meta.env.VITE_BUILD_ID": JSON.stringify("") },
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: {
    outDir: OUT,
    emptyOutDir: true,
    target: "chrome76",
    modulePreload: false,
    cssCodeSplit: false,
    rollupOptions: {
      input: path.resolve(__dirname, "tv-app.html"),
      output: { format: "iife", inlineDynamicImports: true },
    },
  },
});
