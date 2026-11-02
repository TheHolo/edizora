import { useEffect, useLayoutEffect, useMemo, useState, useSyncExternalStore } from 'react';
import type { ReactNode } from 'react';
import { createBrowserHistory, createHashHistory } from 'history';
import { Router } from 'react-router-dom';
import { canNavigate, hasUnsavedChanges, subscribeUnsaved } from '../navigation';

/** Browser URLs remain unchanged; the packaged application uses hash history. */
export function AppRouter({ mode, children }: { mode: 'web' | 'desktop'; children: ReactNode }) {
  const history = useMemo(() => mode === 'desktop' ? createHashHistory() : createBrowserHistory(), [mode]);
  const [state, setState] = useState(() => ({ action: history.action, location: history.location }));
  const unsaved = useSyncExternalStore(subscribeUnsaved, hasUnsavedChanges, () => false);
  useLayoutEffect(() => history.listen(setState), [history]);
  useEffect(() => {
    if (!unsaved) return;
    const unblock = history.block(transition => {
      if (canNavigate()) { unblock(); transition.retry(); }
    });
    return unblock;
  }, [history, unsaved]);
  return <Router location={state.location} navigationType={state.action} navigator={history}>{children}</Router>;
}
