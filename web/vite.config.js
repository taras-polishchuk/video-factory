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
      // /auth/csrf (CSRF token for browser-side writes) and the
      // /auth/oauth/* links live on FastAPI too; Caddy already
      // forwards /auth/* in production.
      '/auth': {
        target: apiTarget,
        changeOrigin: false
      }
    }
  }
});