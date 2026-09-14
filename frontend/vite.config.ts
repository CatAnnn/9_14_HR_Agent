import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

function positiveInteger(value: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export default defineConfig(({ command }) => {
  const secureHmr = command === 'serve' && process.env.VITE_HMR_SECURE === 'true';
  const frontendHost = process.env.FRONTEND_HTTPS_HOST?.trim() || 'localhost';
  const frontendPort = positiveInteger(process.env.FRONTEND_HTTPS_PORT, 7443);
  const usePolling = process.env.VITE_USE_POLLING === 'true';

  return {
    publicDir: '../data/frontend',
    plugins: [react()],
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (
              id.includes('/node_modules/react/')
              || id.includes('/node_modules/react-dom/')
              || id.includes('/node_modules/react-router/')
              || id.includes('/node_modules/react-router-dom/')
              || id.includes('/node_modules/scheduler/')
            ) {
              return 'react-vendor';
            }
            return undefined;
          }
        },
      },
    },
    server: {
      host: '0.0.0.0',
      port: 8080,
      strictPort: true,
      allowedHosts: ['localhost', '127.0.0.1', frontendHost],
      hmr: secureHmr
        ? {
            protocol: 'wss',
            host: frontendHost,
            clientPort: frontendPort,
          }
        : undefined,
      watch: usePolling
        ? {
            usePolling: true,
            interval: positiveInteger(process.env.VITE_WATCH_INTERVAL_MS, 250),
          }
        : undefined,
    },
    preview: {
      host: '0.0.0.0',
      port: 8080,
    },
  };
});
