import { createContext, useContext } from 'react';
import type { AnchorHTMLAttributes } from 'react';

export const NavigationContext = createContext<(path: string) => void>(() => undefined);
let unsaved = false;
export function setUnsaved(value: boolean) {
  unsaved = value;
  const app = window.Telegram?.WebApp;
  if(app?.initData) { if(value)app.enableClosingConfirmation?.();else app.disableClosingConfirmation?.(); }
}
export function canNavigate() { return !unsaved || window.confirm('Есть несохранённые изменения. Покинуть редактор?'); }
export function useNavigate() { return useContext(NavigationContext); }
export function Link({ href = '#', onClick, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  const navigate = useNavigate();
  return <a {...props} href={href} onClick={event => { onClick?.(event); if (!event.defaultPrevented && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0 && href.startsWith('/')) { event.preventDefault(); navigate(href); } }} />;
}
