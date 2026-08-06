import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

const widgetRoot = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  root: widgetRoot,
  build: {
    outDir: widgetRoot + 'dist',
    emptyOutDir: true,
    sourcemap: true,
    lib: {
      entry: widgetRoot + 'src/index.ts',
      name: 'NeryvaWidget',
      formats: ['es', 'umd'],
      fileName: (format) =>
        format === 'es' ? 'neryva-widget.js' : 'neryva-widget.umd.cjs',
    },
  },
  server: {
    port: 3001,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
