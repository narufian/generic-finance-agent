import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Built assets are served by FastAPI at /chat; in dev, Vite proxies the API and shared stylesheet.
export default defineConfig({
  base: '/chat/',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:3000',
      '/styles.css': 'http://127.0.0.1:3000',
    },
  },
});
