import { t } from './i18n.ts';
import type { ApiErrorData, Session } from './types';
import { TransportUnavailable, WebTransport } from './platform/transport.ts';
import type { ApiTransport, DesktopFile, MediaResource, UploadSource } from './platform/transport.ts';

let csrfToken = '';
let bearerToken = '';
let sessionScope = 'anonymous';
type PendingMutation = { key: string; active: number; uncertain: boolean };
const pendingKeys = new Map<string, PendingMutation>();
const webTransport = new WebTransport(() => ({ csrf: csrfToken, bearer: bearerToken }));
let transport: ApiTransport = webTransport;
export function setApiTransport(value: ApiTransport) { transport = value; }
export function resetApiTransport() { transport = webTransport; }
export function setSession(session: Session) {
  csrfToken = session.csrf_token || '';
  bearerToken = session.token || session.access_token || '';
  sessionScope = `${typeof session.user.id}:${session.user.id}`;
}
export function clearSession() { csrfToken = ''; bearerToken = ''; sessionScope = 'anonymous'; }
export function getSessionScope() { return sessionScope; }
export class ApiError extends Error {
  status: number;
  data: ApiErrorData;
  constructor(status: number, data: ApiErrorData) { super(t(data.message)); this.status = status; this.data = data; }
}
async function mutationIdentity(scope: string, method: string, path: string, payload?: string) {
  // Keep a digest, not submitted text, passwords or other secret fields, when
  // an unknown outcome requires the same idempotency key on a later retry.
  const bytes = new TextEncoder().encode(JSON.stringify([scope, method, path, payload ?? null]));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}
export async function api<T>(path: string, options: { method?: string; body?: unknown; key?: string; signal?: AbortSignal } = {}): Promise<T> {
  const method = options.method || 'GET';
  const mutation = !['GET', 'HEAD'].includes(method);
  const payload = options.body === undefined ? undefined : JSON.stringify(options.body);
  const digest = mutation && !path.startsWith('/auth/') ? await mutationIdentity(sessionScope, method, path, payload) : '';
  const identity = digest ? `${digest}:${options.key || ''}` : '';
  let pending: PendingMutation | undefined;
  if (identity) {
    pending = pendingKeys.get(identity) || { key: options.key || crypto.randomUUID(), active: 0, uncertain: false };
    pending.active++;
    pendingKeys.set(identity, pending);
  }
  const key = mutation ? options.key || pending?.key || crypto.randomUUID() : undefined;
  let accepted = false;
  try {
    let response;
    try { response = await transport.request({ method, path, body: options.body, payload, key, signal: options.signal }); }
    catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      if (!(error instanceof TransportUnavailable)) throw error;
      throw new ApiError(0, { code: 'offline', message: t("Нет связи с панелью. Введённые данные сохранены в открытой вкладке. Проверьте соединение и повторите действие."), retryable: true });
    }
    const data = response.body as { error?: ApiErrorData; detail?: string } | null;
    if (response.status < 200 || response.status >= 300) {
      const detail: Partial<ApiErrorData> | null = data?.error || (typeof data?.detail === 'string' ? { message: data.detail } : null);
      if (detail?.code === 'reauth_required') window.dispatchEvent(new Event('publisher-reauth-needed'));
      if (response.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event('publisher-auth-needed'));
      throw new ApiError(response.status, { code: detail?.code || `http_${response.status}`, message: detail?.message || (response.status === 401 ? t("Войдите, чтобы продолжить работу.") : response.status === 403 ? t("Нет разрешения на это действие.") : response.status === 503 ? t("Приложение временно занято обслуживанием. Сохранённые операции продолжают выполняться.") : t("Не удалось выполнить запрос ({0}).", [response.status])), ...detail });
    }
    const normalized = normalize(data);
    accepted = true;
    return (Array.isArray(normalized) && ['/deployment/nodes','/backups'].includes(path.split('?')[0]) ? {items: normalized} : normalized) as T;
  } finally {
    if (pending) {
      pending.active--;
      pending.uncertain = !accepted;
      if (!pending.active && !pending.uncertain && pendingKeys.get(identity) === pending) pendingKeys.delete(identity);
    }
  }
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
export async function acquireMedia(path: string, signal: AbortSignal): Promise<MediaResource> {
  if (transport.media) return transport.media(path, signal);
  if (bearerToken) {
    const blob = await mediaBlob(path, signal);
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
    const url = URL.createObjectURL(blob);
    return { url, release: () => URL.revokeObjectURL(url) };
  }
  const url = safeURL(path);
  if (!url) throw new Error(t("Изображение недоступно"));
  return { url, release: () => undefined };
}
export function hasNativeFilePicker() { return !!transport.pickFiles; }
export async function pickNativeFiles() {
  if (!transport.pickFiles) throw new Error('Native file selection is unavailable');
  return transport.pickFiles();
}
export async function releaseNativeFiles(files: UploadSource[]) {
  const native = files.filter((file): file is DesktopFile => 'kind' in file && file.kind === 'desktop');
  if (native.length && transport.releaseFiles) await transport.releaseFiles(native);
}
export async function upload(file: UploadSource) {
  if ('kind' in file && file.kind === 'desktop') {
    if (!transport.uploadFile) throw new Error('Native upload is unavailable');
    const response = await transport.uploadFile(file);
    const data = response.body as { id?: string; error?: ApiErrorData } | null;
    if (response.status < 200 || response.status >= 300) throw new ApiError(response.status, data?.error || { code: 'upload_failed', message: t("Не удалось загрузить файл. Прежние медиа сохранены.") });
    if (!data?.id) throw new Error(t("Не удалось загрузить файл. Прежние медиа сохранены."));
    return { id: data.id };
  }
  if (transport.pickFiles) throw new Error('Desktop uploads require a native file selection');
  if (!(file instanceof File)) throw new Error('Invalid browser upload');
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
