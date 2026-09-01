// Single aggregation point for all locale resources.
// Todos 7-10 will extend this file as new namespaces/locale JSON files are added.
import zhCommon from '../locales/zh/common.json';
import zhErrors from '../locales/zh/errors.json';
import enCommon from '../locales/en/common.json';
import enErrors from '../locales/en/errors.json';

export const resources = {
  zh: {
    common: zhCommon,
    errors: zhErrors,
  },
  en: {
    common: enCommon,
    errors: enErrors,
  },
} as const;
