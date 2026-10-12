import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // Override the API address with VITE_API_TARGET (e.g. in .env.local) when port 8000 is taken.
  const env = loadEnv(mode, process.cwd(), "");
  const backend = { target: env.VITE_API_TARGET || "http://localhost:8000", changeOrigin: true };

  return {
    plugins: [react()],
    server: {
      proxy: {
        "/query": backend,
        "/hitl": backend,
        "/admin": backend,
        "/auth": backend,
        "/dictionary": backend,
      },
    },
  };
});
