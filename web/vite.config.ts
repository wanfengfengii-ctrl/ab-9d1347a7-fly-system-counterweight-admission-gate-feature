/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // 浏览器走相对路径 /api，由 dev server 代理到 API 服务
      '/api': {
        target: process.env.API_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    testTimeout: 20000,
    env: {
      // Vitest 直连真实 API（容器内 API_URL=http://api:8000），不使用任何假接口
      VITE_API_URL: process.env.API_URL ?? 'http://localhost:8000',
    },
  },
});
