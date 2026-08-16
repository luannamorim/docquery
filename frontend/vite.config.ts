import { defineConfig } from "vite";

// The build lands inside the Python package, which is what lets app.py mount it
// with a path relative to itself and what makes it ship in the image without a
// second copy step.
export default defineConfig({
  build: {
    outDir: "../src/docquery/api/static",
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` proxies the API so the browser still sees one origin and
    // the same-origin assumption holds in development too.
    proxy: Object.fromEntries(
      ["/query", "/conversations", "/ingest", "/health", "/config"].map((path) => [
        path,
        { target: "http://localhost:8000", changeOrigin: true },
      ]),
    ),
  },
});
