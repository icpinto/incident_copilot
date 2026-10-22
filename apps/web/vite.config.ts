import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: { rollupOptions: { output: { manualChunks: { charts: ['recharts'], react: ['react', 'react-dom', 'react-router-dom'], query: ['@tanstack/react-query'] } } } },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8080', '/health': 'http://127.0.0.1:8080' },
  },
  test: { environment: 'jsdom', setupFiles: ['./src/test-setup.ts'], css: false },
});
