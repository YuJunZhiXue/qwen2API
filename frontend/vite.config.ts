import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from "path"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5174,
    proxy: {
      '/api':       { target: 'http://localhost:7860', changeOrigin: true, timeout: 0 },
      '/v1':        { target: 'http://localhost:7860', changeOrigin: true, timeout: 0 },
      '/anthropic': { target: 'http://localhost:7860', changeOrigin: true, timeout: 0 },
      '/v1beta':    { target: 'http://localhost:7860', changeOrigin: true, timeout: 0 },
    }
  }
})
