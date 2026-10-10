import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    port: 5173,
    host: '127.0.0.1',
    strictPort: false,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: false
      },
      // Production Caddy sends /auth/* (csrf token, OIDC start/callback)
      // to the API; mirror that in dev so the browser client can fetch
      // /auth/csrf. SvelteKit has no /auth route of its own.
      '/auth': {
        target: apiTarget,
        changeOrigin: false
      }
    }
  }
});