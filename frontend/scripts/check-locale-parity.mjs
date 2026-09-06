#!/usr/bin/env node
/**
 * Locale key parity checker (zh vs en) — no external dependencies.
 *
 * Usage (from frontend/):
 *   node scripts/check-locale-parity.mjs [localesDir]
 *
 * localesDir defaults to "src/locales". It must contain one subdirectory per
 * locale (at minimum "zh" and "en"), each holding JSON files, one per
 * namespace. Values may be nested; nested objects are flattened to dotted
 * keys.
 *
 * Assertions:
 *   1. File sets: zh/ and en/ contain the exact same set of namespace files.
 *   2. Key sets: for every namespace, zh and en expose the same set of BASE
 *      keys. i18next plural-suffix segments (_zero, _one, _two, _few, _many,
 *      _other) are normalized away before comparison, because the number of
 *      plural forms is locale-specific (en requires _one+_other, zh has a
 *      single form). A base key present in one locale but missing in the
 *      other is a hard error.
 *   3. Values: every leaf value in BOTH locales is a non-empty string.
 *      Empty, whitespace-only, or non-string leaves are hard errors.
 *
 * Known, already-tracked gaps may be listed in scripts/locale-parity-baseline.json
 * (a ratchet: an entry tolerates exactly one finding; any finding NOT listed
 * fails the check, and any listed entry that no longer matches a real finding
 * also fails the check, so the baseline shrinks as gaps get fixed).
 *
 * Prints a summary count and exits 0 on success, 1 on any failure.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ZH = 'zh';
const EN = 'en';
const PLURAL_SUFFIXES = ['zero', 'one', 'two', 'few', 'many', 'other'];
const BASELINE_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  'locale-parity-baseline.json'
);

const localesDir = process.argv[2] || 'src/locales';
const problems = [];

function loadBaseline() {
  let raw;
  try {
    raw = readFileSync(BASELINE_PATH, 'utf8');
  } catch {
    return { missingKeys: new Set(), emptyValues: new Set() };
  }
  const parsed = JSON.parse(raw);
  return {
    missingKeys: new Set(parsed.missingKeys ?? []),
    emptyValues: new Set(parsed.emptyValues ?? []),
  };
}

/** Recursively flatten a JSON object into a Map of dotted key -> leaf value. */
function flatten(obj, prefix = '', out = new Map()) {
  for (const [key, value] of Object.entries(obj)) {
    const fullKey = prefix ? `${prefix}.${key}` : key;
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      flatten(value, fullKey, out);
    } else {
      out.set(fullKey, value);
    }
  }
  return out;
}

/**
 * Strip an i18next plural suffix from the last key segment, e.g.
 * "preview.chaptersTitle_other" -> "preview.chaptersTitle".
 */
function baseKey(key) {
  const seg = key.split('.').pop();
  for (const suffix of PLURAL_SUFFIXES) {
    if (seg.endsWith(`_${suffix}`)) return key.slice(0, -(suffix.length + 1));
  }
  return key;
}

function readNamespaces(locale) {
  const dir = join(localesDir, locale);
  try {
    return new Set(readdirSync(dir).filter((f) => f.endsWith('.json')));
  } catch (err) {
    console.error(`ERROR: cannot read locale directory ${dir}: ${err.message}`);
    process.exit(1);
  }
}

function loadLocaleMap(locale) {
  const map = new Map(); // namespace -> flattened Map
  for (const file of readNamespaces(locale)) {
    try {
      const parsed = JSON.parse(readFileSync(join(localesDir, locale, file), 'utf8'));
      map.set(file, flatten(parsed));
    } catch (err) {
      problems.push(`[${locale}/${file}] invalid JSON: ${err.message}`);
    }
  }
  return map;
}

function checkValues(locale, file, flat) {
  for (const [key, value] of flat) {
    if (typeof value !== 'string') {
      problems.push(`invalidValue:${locale}/${file}:${key} (non-string value ${JSON.stringify(value)})`);
    } else if (value.trim() === '') {
      problems.push(`emptyValue:${locale}/${file}:${key}`);
    }
  }
}

const baseline = loadBaseline();
const usedBaseline = { missingKeys: new Set(), emptyValues: new Set() };

const zhFiles = readNamespaces(ZH);
const enFiles = readNamespaces(EN);

for (const file of [...zhFiles].filter((f) => !enFiles.has(f)).sort()) {
  problems.push(`fileSet:"${file}" exists in ${ZH}/ but is missing in ${EN}/`);
}
for (const file of [...enFiles].filter((f) => !zhFiles.has(f)).sort()) {
  problems.push(`fileSet:"${file}" exists in ${EN}/ but is missing in ${ZH}/`);
}

const zh = loadLocaleMap(ZH);
const en = loadLocaleMap(EN);

const commonFiles = [...zhFiles].filter((f) => enFiles.has(f)).sort();
for (const file of commonFiles) {
  const zhFlat = zh.get(file);
  const enFlat = en.get(file);
  if (!zhFlat || !enFlat) continue; // JSON parse error already reported

  checkValues(ZH, file, zhFlat);
  checkValues(EN, file, enFlat);

  const zhBase = new Set([...zhFlat.keys()].map(baseKey));
  const enBase = new Set([...enFlat.keys()].map(baseKey));
  for (const key of [...zhBase].filter((k) => !enBase.has(k)).sort()) {
    problems.push(`missingKey:${file}:${key} (missing in ${EN})`);
  }
  for (const key of [...enBase].filter((k) => !zhBase.has(k)).sort()) {
    problems.push(`missingKey:${file}:${key} (missing in ${ZH})`);
  }
}

// Ratchet: split findings into baseline-tolerated vs actionable. Unknown
// categories and stale baseline entries are always actionable.
const tolerated = [];
const actionable = [];
for (const problem of problems) {
  const category = problem.slice(0, problem.indexOf(':'));
  const id = problem.slice(problem.indexOf(':') + 1);
  if (category === 'missingKey' && baseline.missingKeys.has(id)) {
    usedBaseline.missingKeys.add(id);
    tolerated.push(problem);
  } else if (category === 'emptyValue' && baseline.emptyValues.has(id)) {
    usedBaseline.emptyValues.add(id);
    tolerated.push(problem);
  } else {
    actionable.push(problem);
  }
}
for (const id of baseline.missingKeys) {
  if (!usedBaseline.missingKeys.has(id)) actionable.push(`stale baseline entry missingKeys:"${id}" (no matching finding; remove it from locale-parity-baseline.json)`);
}
for (const id of baseline.emptyValues) {
  if (!usedBaseline.emptyValues.has(id)) actionable.push(`stale baseline entry emptyValues:"${id}" (no matching finding; remove it from locale-parity-baseline.json)`);
}

const totalKeysZh = [...zh.values()].reduce((sum, m) => sum + m.size, 0);
const totalKeysEn = [...en.values()].reduce((sum, m) => sum + m.size, 0);

if (actionable.length > 0) {
  console.error(`i18n locale parity check FAILED with ${actionable.length} problem(s):\n`);
  for (const p of actionable) console.error(`  - ${p}`);
  if (tolerated.length > 0) {
    console.error(`\n(${tolerated.length} known gap(s) tolerated via scripts/locale-parity-baseline.json — fix and remove those entries.)`);
  }
  console.error(`\nChecked ${commonFiles.length} namespace files (zh: ${totalKeysZh} keys, en: ${totalKeysEn} keys).`);
  process.exit(1);
}

console.log(
  `i18n locale parity check OK: ${commonFiles.length} namespace files, zh ${totalKeysZh} keys / en ${totalKeysEn} keys, ` +
  `${tolerated.length} known gap(s) tracked in locale-parity-baseline.json, all other values non-empty strings.`
);
