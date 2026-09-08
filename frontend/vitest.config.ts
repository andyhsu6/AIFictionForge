import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Vitest is UI-only: no build output, no proxy needed. Kept in sync with
// vite.config.ts plugins; path aliases intentionally absent (none used).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false, // explicit `import { describe, it, expect } from 'vitest'` keeps `tsc -b` hermetic
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
});
