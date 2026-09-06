/**
 * todo 26 (b) + (d-locale): zh/en key-set equality and locale-internal checks.
 *
 * Executes scripts/check-locale-parity.mjs (the same file CI runs) and asserts
 * exit 0 — a single source of truth, no duplicated parity logic. The script
 * covers: file-set equality, base-key-set equality per namespace (plural
 * suffixes normalized), non-empty string values in BOTH locales, and
 * plural-group completeness per locale convention (en _one/_other complete;
 * zh single-form values identical).
 */
import { execFile } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { describe, expect, it } from 'vitest';

const execFileAsync = promisify(execFile);
const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

describe('locale parity via scripts/check-locale-parity.mjs (todo 26b + 26d-locale)', () => {
  it('passes with zero baseline entries (baseline file removed)', async () => {
    const { stdout } = await execFileAsync('node', ['scripts/check-locale-parity.mjs'], {
      cwd: FRONTEND_ROOT,
    });
    expect(stdout).toContain('i18n locale parity check OK');
    // The ratchet is gone: the summary must no longer advertise tolerated gaps.
    expect(stdout).not.toContain('known gap');
  });

  it('exists as the CI-invoked checker (single source of truth)', async () => {
    const { stdout } = await execFileAsync('node', ['scripts/check-locale-parity.mjs', 'src/locales'], {
      cwd: FRONTEND_ROOT,
    });
    expect(stdout).toContain('plural groups complete');
  });
});
