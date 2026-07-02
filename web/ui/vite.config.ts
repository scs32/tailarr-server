import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The controller serves the built assets from STATIC_DIR at the web root.
// Absolute base so /assets/* resolve from any nested client route (e.g.
// /install/sonarr). In dev, `npm run dev` proxies /api to app.py on :8080.
export default defineConfig({
  plugins: [react()],
  base: "/",
  server: {
    proxy: {
      "/api": "http://localhost:8080",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
