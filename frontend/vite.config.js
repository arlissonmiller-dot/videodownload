import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/auth": "http://localhost:8000",
      "/config": "http://localhost:8000",
      "/info": "http://localhost:8000",
      "/download-jobs": "http://localhost:8000",
      "/download": "http://localhost:8000",
    },
  },
});
