import { t } from './i18n';
import { createContext, useContext } from 'react';
import type { AnchorHTMLAttributes } from 'react';
import { useHref } from 'react-router-dom';

export const NavigationContext = createContext<(path: string) => void>(() => undefined);
let unsaved = false;
const unsavedListeners = new Set<() => void>();
export function subscribeUnsaved(listener: () => void) { unsavedListeners.add(listener); return () => { unsavedListeners.delete(listener); }; }
export function hasUnsavedChanges() { return unsaved; }
export function setUnsaved(value: boolean) {
  const changed = unsaved !== value;
  unsaved = value;
  if(changed)unsavedListeners.forEach(listener=>listener());
  const app = window.Telegram?.WebApp;
  if(app?.initData) { if(value)app.enableClosingConfirmation?.();else app.disableClosingConfirmation?.(); }
}
export function canNavigate() { return !unsaved || window.confirm(t("Есть несохранённые изменения. Покинуть редактор?")); }
export function useNavigate() { return useContext(NavigationContext); }
export function Link({ href = '#', onClick, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  const navigate = useNavigate();
  const internal = href.startsWith('/');
  const routedHref = useHref(internal ? href : '/');
  return <a {...props} href={internal ? routedHref : href} onClick={event => { onClick?.(event); if (!event.defaultPrevented && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0 && internal) { event.preventDefault(); navigate(href); } }} />;
}
