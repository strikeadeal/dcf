import { defineConfig } from 'vite';

// Dev: `npm run dev` on :5173 proxies the API to uvicorn on :8000.
export default defineConfig({
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
});
