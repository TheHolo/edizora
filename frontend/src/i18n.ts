import { english } from './messages.ts';
import { serverEnglish } from './server-messages.ts';

export type Language = 'en' | 'ru';
const storageKey = 'edizora-language';

export function getLanguage(): Language {
  try { return localStorage.getItem(storageKey) === 'ru' ? 'ru' : 'en'; }
  catch { return 'en'; }
}

export function setLanguage(language: Language) {
  // Save before reloading so every module-level label uses the same language.
  // If storage is unavailable, let the caller show an error instead of losing the choice.
  localStorage.setItem(storageKey, language);
}

export function locale() { return getLanguage() === 'ru' ? 'ru-RU' : 'en-US'; }

export function t(source: string, values: readonly unknown[] = []): string {
  const message = getLanguage() === 'en' ? english[source] ?? serverEnglish[source] ?? source : source;
  return message.replace(/\{(\d+)\}/g, (match, index: string) =>
    Number(index) < values.length ? String(values[Number(index)]) : match);
}

if (typeof document !== 'undefined') document.documentElement.lang = getLanguage();
