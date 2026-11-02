/** The JSON part of the API contract shared by browser and desktop clients. */
export type TransportRequest = {
  path: string;
  method: string;
  body?: unknown;
  payload?: string;
  key?: string;
  signal?: AbortSignal;
};

export type TransportResponse = { status: number; body: unknown; requestId?: string };
export type MediaResource = { url: string; release: () => void };
export type DesktopFile = { kind: 'desktop'; id: string; name: string; size: number };
export type UploadSource = File | DesktopFile;

export interface ApiTransport {
  request(request: TransportRequest): Promise<TransportResponse>;
  media?(path: string, signal?: AbortSignal): Promise<MediaResource>;
  pickFiles?(): Promise<DesktopFile[]>;
  uploadFile?(file: DesktopFile): Promise<TransportResponse>;
  releaseFiles?(files: DesktopFile[]): Promise<void>;
}

/** Transport failures do not prove that a mutation was rejected by the server. */
export class TransportUnavailable extends Error {}

type WebCredentials = { csrf: string; bearer: string };

export class WebTransport implements ApiTransport {
  private readonly credentials: () => WebCredentials;
  constructor(credentials: () => WebCredentials) { this.credentials = credentials; }

  async request(request: TransportRequest): Promise<TransportResponse> {
    const { csrf, bearer } = this.credentials();
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (bearer) headers.Authorization = `Bearer ${bearer}`;
    if (request.payload !== undefined) headers['Content-Type'] = 'application/json';
    if (!['GET', 'HEAD'].includes(request.method)) {
      if (csrf) headers['X-CSRF-Token'] = csrf;
      if (request.key) headers['Idempotency-Key'] = request.key;
    }
    let response: Response;
    try {
      response = await fetch(`/api/v1${request.path}`, {
        method: request.method, headers, body: request.payload,
        credentials: 'same-origin', signal: request.signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      throw new TransportUnavailable('The web transport is unavailable', { cause: error });
    }
    const type = response.headers.get('content-type') || '';
    const body: unknown = response.status === 204 ? null : type.includes('json') ? await response.json() : null;
    return { status: response.status, body };
  }
}

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
type DesktopResponse = { status: number; body: unknown; request_id?: string | null };

/** Session cookies and CSRF remain in Rust; renderer-supplied headers are never forwarded. */
export class DesktopTransport implements ApiTransport {
  private readonly invoke: Invoke;
  constructor(invoke: Invoke) { this.invoke = invoke; }

  async request(request: TransportRequest): Promise<TransportResponse> {
    if (request.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const pending = this.invoke<DesktopResponse>('desktop_request', {
      method: request.method, path: `/api/v1${request.path}`,
      body: request.body ?? null, key: request.key ?? null,
    });
    let response: DesktopResponse;
    try { response = await abortWaiting(pending, request.signal); }
    catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      throw new TransportUnavailable('The desktop transport is unavailable', { cause: error });
    }
    return { status: response.status, body: response.body, requestId: response.request_id || undefined };
  }

  async media(path: string, signal?: AbortSignal): Promise<MediaResource> {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const url = await this.invoke<string>('desktop_media', { path });
    const release = () => { void this.invoke('desktop_revoke_media', { url }).catch(() => undefined); };
    // Even if the caller has gone away during IPC, revoke the opaque handle.
    if (signal?.aborted) { release(); throw new DOMException('Aborted', 'AbortError'); }
    return { url, release };
  }

  async pickFiles(): Promise<DesktopFile[]> {
    const files = await this.invoke<Array<Omit<DesktopFile, 'kind'>>>('desktop_pick_files');
    return files.map(file => ({ ...file, kind: 'desktop' }));
  }

  async uploadFile(file: DesktopFile): Promise<TransportResponse> {
    const result = await this.invoke<DesktopResponse>('desktop_upload', { id: file.id, requestId: crypto.randomUUID() });
    return { status: result.status, body: result.body, requestId: result.request_id || undefined };
  }

  async releaseFiles(files: DesktopFile[]): Promise<void> {
    if (files.length) await this.invoke('desktop_release_files', { ids: files.map(file => file.id) });
  }
}

// Cancellation stops the caller waiting. A request already handed to the
// bridge may still finish, so a mutation must keep its idempotency key.
function abortWaiting<T>(pending: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return pending;
  return new Promise((resolve, reject) => {
    const abort = () => { cleanup(); reject(new DOMException('Aborted', 'AbortError')); };
    const cleanup = () => signal.removeEventListener('abort', abort);
    signal.addEventListener('abort', abort, { once: true });
    pending.then(value => { cleanup(); resolve(value); }, error => { cleanup(); reject(error); });
    if (signal.aborted) abort();
  });
}
