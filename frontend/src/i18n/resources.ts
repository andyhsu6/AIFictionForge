// Single aggregation point for all locale resources.
// Todos 7-10 will extend this file as new namespaces/locale JSON files are added.
import zhCommon from '../locales/zh/common.json';
import zhErrors from '../locales/zh/errors.json';
import zhCharacters from '../locales/zh/characters.json';
import zhSettings from '../locales/zh/settings.json';
import enCommon from '../locales/en/common.json';
import enErrors from '../locales/en/errors.json';
import enCharacters from '../locales/en/characters.json';
import enSettings from '../locales/en/settings.json';

export const resources = {
  zh: {
    common: zhCommon,
    errors: zhErrors,
    characters: zhCharacters,
    settings: zhSettings,
  },
  en: {
    common: enCommon,
    errors: enErrors,
    characters: enCharacters,
    settings: enSettings,
  },
} as const;
