import { useCallback, useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { useQuery as useServerQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, getSessionScope } from './api';
import { apiQueryKey, apiQueryOptions } from './shared/api/query';

export function useQuery<T>(path: string | null, interval = 0) {
  const client = useQueryClient();
  const scope = getSessionScope();
  const query = useServerQuery(apiQueryOptions<T>(scope, path, interval));
  const reload = useCallback(() => {
    if (path) void client.invalidateQueries({ queryKey: apiQueryKey(scope, path), exact: true });
  }, [client, path, scope]);
  const setData: Dispatch<SetStateAction<T | null>> = useCallback(value => {
    if (!path) return;
    client.setQueryData<T | null>(apiQueryKey(scope, path), old =>
      typeof value === 'function' ? (value as (current: T | null) => T | null)(old ?? null) : value,
    );
  }, [client, path, scope]);
  return { data: query.data ?? null, error: query.error, loading: !!path && query.isLoading, reload, setData };
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
