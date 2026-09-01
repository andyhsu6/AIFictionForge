// Single aggregation point for all locale resources.
// Todos 7-10 will extend this file as new namespaces/locale JSON files are added.
import zhCommon from '../locales/zh/common.json';
import zhErrors from '../locales/zh/errors.json';
import zhChapters from '../locales/zh/chapters.json';
import zhCharacters from '../locales/zh/characters.json';
import zhOutline from '../locales/zh/outline.json';
import zhSettings from '../locales/zh/settings.json';
import enCommon from '../locales/en/common.json';
import enErrors from '../locales/en/errors.json';
import enChapters from '../locales/en/chapters.json';
import enCharacters from '../locales/en/characters.json';
import enOutline from '../locales/en/outline.json';
import enSettings from '../locales/en/settings.json';

export const resources = {
  zh: {
    common: zhCommon,
    errors: zhErrors,
    chapters: zhChapters,
    characters: zhCharacters,
    outline: zhOutline,
    settings: zhSettings,
  },
  en: {
    common: enCommon,
    errors: enErrors,
    chapters: enChapters,
    characters: enCharacters,
    outline: enOutline,
    settings: enSettings,
  },
} as const;
