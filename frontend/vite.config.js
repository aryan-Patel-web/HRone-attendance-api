import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard calls "/api/..." and Vite forwards it to the FastAPI server (no CORS needed in development).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
