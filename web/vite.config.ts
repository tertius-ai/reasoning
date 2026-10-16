import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Served pages carry `script-src 'self'` as a response header, so a built
// entry page must not depend on an inline script. JSON data blocks are inert.
const INLINE_SCRIPT = /<script\b(?![^>]*\bsrc\s*=)(?![^>]*\btype\s*=\s*["']?application\/(?:ld\+)?json)[^>]*>/i;
const INLINE_HANDLER = /<[^>]+\son[a-z]+\s*=/i;

export default defineConfig({
  plugins: [react(), {
    name: "reasoning-no-inline-scripts",
    enforce: "post",
    generateBundle(_options, bundle) {
      for (const output of Object.values(bundle)) {
        if (output.type !== "asset" || !output.fileName.endsWith(".html")) continue;
        const html = String(output.source);
        if (INLINE_SCRIPT.test(html) || INLINE_HANDLER.test(html) || /javascript:/i.test(html)) {
          this.error(`${output.fileName} contains inline script, which script-src 'self' blocks`);
        }
      }
    },
  }, {
    name: "reasoning-license-material",
    generateBundle() {
      for (const [source, target] of [["LICENSE", "licenses/Reasoning-Apache-2.0.txt"], ["NOTICE", "licenses/Reasoning-NOTICE.txt"]]) {
        this.emitFile({ type: "asset", fileName: target, source: readFileSync(new URL(`../${source}`, import.meta.url), "utf8") });
      }
    },
  }],
  build: {
    manifest: true,
    rollupOptions: {
      input: {
        workbench: fileURLToPath(new URL("index.html", import.meta.url)),
        organization: fileURLToPath(new URL("organization.html", import.meta.url)),
      },
      // The public server caches assets/ files as immutable only because every name carries a content hash.
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
        // The static blog's math enhancement needs only KaTeX and the color-mode helper. Their own chunks keep it
        // from importing React or the Markdown renderer through a shared chunk.
        codeSplitting: {
          groups: [
            { name: "katex", test: /[\\/]node_modules[\\/]katex[\\/].*\.m?js$/ },
            { name: "appearance", test: /[\\/]src[\\/]appearance\.ts$/ },
          ],
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": `http://127.0.0.1:${process.env.REASONING_DEV_BACKEND_PORT || "9119"}`,
    },
  },
});
