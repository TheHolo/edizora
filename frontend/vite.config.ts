import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  build: { sourcemap: false, target: 'es2022', rollupOptions: { input: {
    main: fileURLToPath(new URL('./index.html', import.meta.url)),
    desktopPrototype: fileURLToPath(new URL('./desktop-prototype.html', import.meta.url)),
  } } },
});
