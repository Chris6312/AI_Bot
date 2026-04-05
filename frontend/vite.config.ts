import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, '..'), '')
  const apiTarget = (env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').trim().replace(/\/+$/, '')
  const wsTarget = apiTarget.replace(/^http/i, 'ws')

  return {
    envDir: path.resolve(__dirname, '..'),
    envPrefix: ['VITE_', 'ADMIN_'],
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    define: {
      'import.meta.env.VITE_ADMIN_API_TOKEN': JSON.stringify(env.VITE_ADMIN_API_TOKEN || env.ADMIN_API_TOKEN || ''),
    },
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/ws': {
          target: wsTarget,
          ws: true,
        },
      },
    },
  }
})
