import { t } from './i18n.ts';
import type { Operation } from './types';

export function operationMessage(operation: Operation): string | undefined {
  const items = operation.result?.items;
  if (operation.kind === 'import' && operation.stage_key === 'complete' && items && typeof items === 'object' && !Array.isArray(items)) {
    const failures = Object.values(items).filter(item => item && typeof item === 'object' && item.status === 'failed').length;
    if (failures) return t("Недоступных источников в этом импорте: {0}", [failures]);
  }
  return operation.message;
}
