import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from './api';

export function useQuery<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision(v => v + 1), []);
  useEffect(() => {
    if (!path) { setLoading(false); return; }
    let alive = true; let timeout: ReturnType<typeof setTimeout>; let failures = 0;
    const controller = new AbortController();
    setLoading(true); setError(null);
    async function load() {
      try { const result = await api<T>(path!, { signal: controller.signal }); if (alive) { setData(result); setError(null); failures = 0; } }
      catch (e) { if (alive && !(e instanceof DOMException && e.name === 'AbortError')) { setError(e as ApiError); failures++; } }
      finally { if (alive) { setLoading(false); if (interval) timeout = setTimeout(load, Math.min(30000, interval * 2 ** failures)); } }
    }
    function refresh() { clearTimeout(timeout); void load(); }
    void load(); window.addEventListener('online', refresh);
    return () => { alive = false; clearTimeout(timeout); controller.abort(); window.removeEventListener('online', refresh); };
  }, [path, interval, revision]);
  return { data, error, loading, reload, setData };
}
export function useCommand() {
  const [busy, setBusy] = useState(false); const [error, setError] = useState<ApiError | null>(null);
  const inFlight = useRef(false);
  async function run<T>(task: () => Promise<T>, after?: (result: T) => void) {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    try { const value = await task(); after?.(value); return value; } catch(e) { setError(e as ApiError); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return { busy, error, setError, run };
}
export function useDebounce<T>(value: T, delay: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => { const timer = setTimeout(() => setDebounced(value), delay); return () => clearTimeout(timer); }, [value, delay]);
  return debounced;
}
