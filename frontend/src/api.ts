import { t } from './i18n.ts';
import type { ApiErrorData, Session } from './types';

let csrfToken = '';
let bearerToken = '';
const pendingKeys = new Map<string, string>();
export function setSession(session: Session) {
  csrfToken = session.csrf_token || '';
  bearerToken = session.token || session.access_token || '';
}
export function clearSession() { csrfToken = ''; bearerToken = ''; }
export class ApiError extends Error {
  status: number;
  data: ApiErrorData;
  constructor(status: number, data: ApiErrorData) { super(t(data.message)); this.status = status; this.data = data; }
}
export async function api<T>(path: string, options: { method?: string; body?: unknown; key?: string; signal?: AbortSignal } = {}): Promise<T> {
  const method = options.method || 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (bearerToken) headers.Authorization = `Bearer ${bearerToken}`;
  const mutation = !['GET', 'HEAD'].includes(method);
  const payload = options.body === undefined ? undefined : JSON.stringify(options.body);
  const identity = `${method}:${path}:${payload || ''}`;
  if (payload !== undefined) headers['Content-Type'] = 'application/json';
  if (mutation) {
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
    const key = options.key || pendingKeys.get(identity) || crypto.randomUUID();
    pendingKeys.set(identity, key);
    headers['Idempotency-Key'] = key;
  }
  let response: Response;
  try { response = await fetch(`/api/v1${path}`, { method, headers, body: payload, credentials: 'same-origin', signal: options.signal }); }
  catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, { code: 'offline', message: t("Нет связи с панелью. Введённые данные сохранены в открытой вкладке. Проверьте соединение и повторите действие."), retryable: true });
  }
  const contentType = response.headers.get('content-type') || '';
  const data = response.status === 204 ? null : contentType.includes('json') ? await response.json() : null;
  if (!response.ok) {
    const detail = data?.error || (typeof data?.detail === 'string' ? { message: data.detail } : null);
    if (detail?.code === 'reauth_required') window.dispatchEvent(new Event('publisher-reauth-needed'));
    if (response.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event('publisher-auth-needed'));
    throw new ApiError(response.status, { code: detail?.code || `http_${response.status}`, message: detail?.message || (response.status === 401 ? t("Войдите, чтобы продолжить работу.") : response.status === 403 ? t("Нет разрешения на это действие.") : response.status === 503 ? t("Приложение временно занято обслуживанием. Сохранённые операции продолжают выполняться.") : t("Не удалось выполнить запрос ({0}).", [response.status])), ...detail });
  }
  if (mutation) pendingKeys.delete(identity);
  const normalized = normalize(data);
  return (Array.isArray(normalized) && ['/deployment/nodes','/backups'].includes(path.split('?')[0]) ? {items: normalized} : normalized) as T;
}
function normalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalize);
  if (!value || typeof value !== 'object') return value;
  const source = value as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(source)) {
    // Translate diagnostics, never titles, captions, source text, tags, or other user content.
    result[key] = typeof item === 'string' && ['message', 'stage_message', 'error_message', 'blocked_reason', 'warning'].includes(key) ? t(item)
      : Array.isArray(item) && ['reasons', 'warnings'].includes(key) ? item.map(value => typeof value === 'string' ? t(value) : normalize(value))
      : normalize(item);
  }
  if ('telegram_chat_id' in source && 'title' in source && !('content_kind' in source)) {
    result.name = source.title; result.telegram_id = source.telegram_chat_id;
    result.enabled = source.is_enabled; result.paused = source.is_paused;
    result.interval_seconds = source.publish_interval_seconds; result.publication_mode = source.publish_mode;
    result.subscriber_count = source.member_count;
    result.statistics_updated_at = source.member_count_updated_at;
    result.permissions_checked_at = (source.permissions as Record<string,unknown> | undefined)?.checked_at;
  }
  if ('last_verified_backup' in source && source.last_verified_backup && typeof source.last_verified_backup === 'object') result.last_verified_backup = (source.last_verified_backup as Record<string,unknown>).verified_at || (source.last_verified_backup as Record<string,unknown>).updated_at;
  if ('key' in source && 'status' in source && 'title' in source) result.label = typeof source.title === 'string' ? t(source.title) : source.title;
  return result;
}
export function hasBearer() { return !!bearerToken; }
export async function mediaBlob(path: string, signal: AbortSignal) {
  const url = new URL(path,location.origin);
  if(url.origin !== location.origin) throw new Error(t("Медиа должны находиться на сервере приложения"));
  const response = await fetch(url,{credentials:'same-origin',signal,headers:bearerToken?{Authorization:'Bearer '+bearerToken}:{}});
  if(!response.ok) throw new Error(t("Изображение недоступно"));
  return response.blob();
}
export async function upload(file: File) {
  const headers: Record<string, string> = { 'Idempotency-Key': crypto.randomUUID() };
  if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  if (bearerToken) headers.Authorization = `Bearer ${bearerToken}`;
  const body = new FormData(); body.append('file', file);
  const response = await fetch('/api/v1/assets/uploads', { method: 'POST', body, headers, credentials: 'same-origin' });
  const data = await response.json();
  if (!response.ok) throw new ApiError(response.status, data.error || { code: 'upload_failed', message: t("Не удалось загрузить файл. Прежние медиа сохранены.") });
  return data as { id: string };
}
export function safeURL(value?: string | null) {
  if (!value) return undefined;
  try { const url = new URL(value, window.location.origin); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
}
