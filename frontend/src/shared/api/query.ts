import { QueryClient, queryOptions } from '@tanstack/react-query';
import { api } from '../../api.ts';
import type { ApiError } from '../../api.ts';

export const apiQueryKey = (scope: string, path: string | null) => ['api', scope, path] as const;

export function createQueryClient() {
  return new QueryClient({ defaultOptions: {
    queries: {
      staleTime: 15_000,
      gcTime: 5 * 60_000,
      retry: false,
      refetchOnWindowFocus: true,
      refetchIntervalInBackground: false,
      networkMode: 'always',
    },
    mutations: { retry: false },
  } });
}

export const queryClient = createQueryClient();

export function apiQueryOptions<T>(scope: string, path: string | null, interval = 0) {
  return queryOptions<T, ApiError>({
    queryKey: apiQueryKey(scope, path),
    queryFn: ({ signal }) => {
      if (!path) throw new Error('A disabled query has no API path');
      return api<T>(path, { signal });
    },
    enabled: !!path,
    // An inactive tab or native window must not run periodic UI refreshes.
    refetchIntervalInBackground: false,
    refetchInterval: interval ? query => query.state.status === 'error' ? Math.min(30_000, interval * 2) : interval : false,
  });
}
