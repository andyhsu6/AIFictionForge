/**
 * Issue #55 step 3b: guide the user to the settings page when the AI feature failed
 * because of the model itself.
 *
 * Both model guards (`validation.ai_model_not_configured` and
 * `validation.ai_model_below_minimum`) are fixable by the user in
 * Settings -> text model config, and the localized error text already names that
 * page. Naming a page is not a link though: on the paths that cannot open the gate
 * form (SSE streams, background task rows) a toast disappears and the user is left
 * with a failed feature and no route. This module adds the route.
 *
 * One shared call site pattern for HTTP (`api.ts` interceptor) and SSE
 * (`utils/sseClient.ts`). antd keys the notification, so repeated failures from a
 * retry loop replace the previous one instead of stacking.
 */
import { Button } from 'antd';
import i18n from '../i18n';
import { antdNotification } from '../utils/antdApp';
import { isModelGateError } from './errorMapper';

const GUIDANCE_KEY = 'model-gate-guidance';
const SETTINGS_PATH = '/settings';

export function maybeShowModelGateGuidance(code?: string | null): boolean {
  if (!isModelGateError(code)) return false;
  // Already on the settings page: the gate form renders the three numbers inline
  // there, so a sticky notice would only add noise.
  if (window.location.pathname.startsWith(SETTINGS_PATH)) return false;
  antdNotification.warning({
    key: GUIDANCE_KEY,
    message: i18n.t('modelGate.guidanceTitle', { ns: 'common' }),
    description: i18n.t('modelGate.guidanceBody', { ns: 'common' }),
    placement: 'topRight',
    // Sticky: the underlying AI feature stays unusable until the config changes,
    // so an auto-dismissing notice would vanish before it can be acted on.
    duration: 0,
    btn: (
      <Button type="primary" size="small" href={SETTINGS_PATH}>
        {i18n.t('modelGate.goToSettings', { ns: 'common' })}
      </Button>
    ),
  });
  return true;
}
