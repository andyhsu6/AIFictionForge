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
 *      (A value that merely repeats its own key path is NOT detectable here —
 *      it is a translation-quality bug, covered by src/i18n-integrity tests.)
 *   4. Plural-group completeness (per locale convention):
 *        - en (CLDR _one/_other): every base key that has ANY plural-suffixed
 *          member must have BOTH `_one` and `_other`. A group missing a member
 *          renders raw key paths for the missing count category.
 *        - zh (single plural form): all plural-suffixed members of a base key
 *          must carry the IDENTICAL value — the suffix is meaningless in zh,
 *          so divergent values indicate en-style plural text leaked into zh.
 *          (A few registry error-code names bake `_one` into the CODE itself,
 *          e.g. `validation.characters_selected_min_one`; those single
 *          suffixed members are plain keys in practice and pass trivially.)
 *
 * There is no baseline/ratchet file: known gaps must be fixed, not tracked.
 * (The former locale-parity-baseline.json was deleted when the last 87 empty
 * en values were filled; an absent baseline previously meant empty tolerance
 * sets, so deleting the ratchet code changes no behavior on a clean tree.)
 *
 * Prints a summary count and exits 0 on success, 1 on any failure.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const ZH = 'zh';
const EN = 'en';
const PLURAL_SUFFIXES = ['zero', 'one', 'two', 'few', 'many', 'other'];

const localesDir = process.argv[2] || 'src/locales';
const problems = [];

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

/** Suffix of the last key segment if it is a plural form, else null. */
function pluralSuffix(key) {
  const seg = key.split('.').pop();
  for (const suffix of PLURAL_SUFFIXES) {
    if (seg.endsWith(`_${suffix}`)) return suffix;
  }
  return null;
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

/**
 * Plural-group completeness, per locale convention (see header doc).
 * flat: namespace Map (key -> value); file: namespace file name.
 */
function checkPluralGroups(locale, file, flat) {
  // base -> { suffix -> [values] } for suffixed members only
  const groups = new Map();
  for (const [key, value] of flat) {
    const suffix = pluralSuffix(key);
    if (!suffix) continue;
    const base = baseKey(key);
    if (!groups.has(base)) groups.set(base, new Map());
    const bySuffix = groups.get(base);
    if (!bySuffix.has(suffix)) bySuffix.set(suffix, []);
    bySuffix.get(suffix).push(value);
  }

  for (const [base, bySuffix] of groups) {
    if (locale === EN) {
      // en: any suffixed member implies the complete _one/_other group.
      for (const required of ['one', 'other']) {
        if (!bySuffix.has(required)) {
          problems.push(`pluralGroup:${file}:${base} missing _${required} (en requires _one+_other)`);
        }
      }
    } else if (locale === ZH) {
      // zh: single plural form — every suffixed member must be identical.
      const values = [...bySuffix.values()].flat();
      const distinct = new Set(values);
      if (distinct.size > 1) {
        problems.push(`pluralGroup:${file}:${base} has ${distinct.size} divergent zh plural values (zh has a single plural form; suffixes must carry identical text)`);
      }
    }
  }
}

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
  checkPluralGroups(ZH, file, zhFlat);
  checkPluralGroups(EN, file, enFlat);

  const zhBase = new Set([...zhFlat.keys()].map(baseKey));
  const enBase = new Set([...enFlat.keys()].map(baseKey));
  for (const key of [...zhBase].filter((k) => !enBase.has(k)).sort()) {
    problems.push(`missingKey:${file}:${key} (missing in ${EN})`);
  }
  for (const key of [...enBase].filter((k) => !zhBase.has(k)).sort()) {
    problems.push(`missingKey:${file}:${key} (missing in ${ZH})`);
  }
}

const totalKeysZh = [...zh.values()].reduce((sum, m) => sum + m.size, 0);
const totalKeysEn = [...en.values()].reduce((sum, m) => sum + m.size, 0);

if (problems.length > 0) {
  console.error(`i18n locale parity check FAILED with ${problems.length} problem(s):\n`);
  for (const p of problems) console.error(`  - ${p}`);
  console.error(`\nChecked ${commonFiles.length} namespace files (zh: ${totalKeysZh} keys, en: ${totalKeysEn} keys).`);
  process.exit(1);
}

console.log(
  `i18n locale parity check OK: ${commonFiles.length} namespace files, zh ${totalKeysZh} keys / en ${totalKeysEn} keys, ` +
  `all values non-empty strings, plural groups complete per locale convention.`
);
