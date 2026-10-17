import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

// Built to app/ui_next and served same-origin at /app-next/ under a strict CSP
// (script-src 'self'; style-src 'self'): no inline script/style, no data: URIs, no CDN.
export default defineConfig({
  base: "/app-next/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "../app/ui_next",
    emptyOutDir: true,
    assetsInlineLimit: 0,
    cssCodeSplit: false,
    sourcemap: false,
    modulePreload: { polyfill: false },
    rollupOptions: { output: { entryFileNames: "assets/[name]-[hash].js", chunkFileNames: "assets/[name]-[hash].js", assetFileNames: "assets/[name]-[hash][extname]" } },
  },
  server: { port: 5173, proxy: { "/v2": "http://127.0.0.1:8770", "/auth": "http://127.0.0.1:8770" } },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
