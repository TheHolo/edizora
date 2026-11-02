import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { api, clearSession, setApiTransport, setSession } from './api';
import { DesktopTransport } from './platform/transport';
import type { Session } from './types';
import './desktop-prototype.css';

type Phase = 'starting' | 'setup' | 'ready' | 'failed';
type Response = { status: number; body: Record<string, unknown> | null; request_id?: string };
type FileHandle = { id: string; name: string; size: number };
type Entry = { id: string | number; title?: string; name?: string };
type Language = 'ru' | 'en';
type ShutdownPhase = 'idle' | 'stopping' | 'failed';

const words = {
  ru: {
    badge: 'ТЕСТОВАЯ ВЕРСИЯ', language: 'English', boot: 'Запускаем приложение', wait: 'Готовим локальные службы и проверяем установку.',
    slow: 'Запуск занимает больше времени, чем ожидалось. Проверьте журнал и состояние служб.', retry: 'Проверить снова',
    stillStarting: 'Приложение ещё запускается. Дождитесь подтверждения служб и проверьте журнал.',
    retryUnconfirmed: 'Завершение предыдущего запуска не подтверждено. Проверьте журнал перед повторной попыткой.',
    retryFailed: 'Не удалось повторить проверку. Проверьте журнал приложения.',
    failed: 'Не удалось запустить тестовую установку.', failedHelp: 'Убедитесь, что подготовлен отдельный тестовый профиль и доступен backend.',
    setup: 'Настроим Edizora', intro: 'Данные останутся на этом компьютере. После настройки очередь будет на паузе.', mode: 'Как начать',
    new: 'Новая установка', legacy: 'Импорт старого бота', restore: 'Восстановление из копии', checkout: 'Папка старого приложения',
    database: 'Файл базы данных', storage: 'Папка медиа', backup: 'Папка резервной копии', recovery: 'Пароль восстановления, если он задан',
    confirm: 'Прежнее приложение остановлено; очередь будет проверена вручную', telegramId: 'Ваш числовой Telegram ID', password: 'Пароль панели',
    repeat: 'Повторите пароль', setupButton: 'Создать установку', preparing: 'Подготавливаем установку…', login: 'Вход в Edizora',
    loginIntro: 'Используйте Telegram ID и пароль, заданные при настройке.', signIn: 'Войти', signing: 'Входим…',
    prototype: 'Проверка локального приложения', note: 'Тестовый профиль · данные и отправка из рабочей установки не используются.',
    signOut: 'Выйти', channels: 'Каналы', publications: 'Публикации', none: 'Пока ничего нет.', refresh: 'Обновить',
    create: 'Добавить тестовый канал', alias: 'Короткое имя', title: 'Название', chat: 'Telegram chat ID', add: 'Добавить канал',
    upload: 'Проверка изображения', choose: 'Выбрать изображение', uploading: 'Загружаем…', cancel: 'Отменить ожидание', image: 'Загруженное изображение',
    preparingHost: 'Настройка завершена. Запускаем службы…', error: 'Не удалось выполнить действие', invalid: 'Проверьте поля формы.',
    closeNote: 'При закрытии окна приложение продолжит работу в трее. Для остановки используйте «Выйти и остановить».',
    trayTitle: 'Edizora продолжит работу', trayHelp: 'Окно скроется в трей, а очередь продолжит работу на этом компьютере.',
    trayHide: 'Скрыть в трей', trayCancel: 'Оставить окно',
    exit: 'Выйти и остановить', stopping: 'Завершаем работу', stoppingHelp: 'Дожидаемся завершения текущих операций и безопасной остановки служб.',
    stopFailed: 'Не удалось подтвердить остановку', stopFailedHelp: 'Проверьте состояние приложения и повторите попытку. Принудительное завершение не выполнялось.',
    retryStop: 'Повторить остановку', returnApp: 'Вернуться в приложение',
  },
  en: {
    badge: 'TEST BUILD', language: 'Русский', boot: 'Starting Edizora', wait: 'Preparing local services and checking the installation.',
    slow: 'Startup is taking longer than expected. Check diagnostics and service status.', retry: 'Check again',
    stillStarting: 'The application is still starting. Wait for the services and check the log.',
    retryUnconfirmed: 'The previous startup has not been confirmed stopped. Check the log before retrying.',
    retryFailed: 'Could not check again. Check the application log.',
    failed: 'The test installation could not start.', failedHelp: 'Make sure an isolated test profile and the backend are available.',
    setup: 'Set up Edizora', intro: 'Your data stays on this computer. The queue will start paused.', mode: 'How would you like to start?',
    new: 'New installation', legacy: 'Import an existing bot', restore: 'Restore a backup', checkout: 'Old application folder',
    database: 'Database file', storage: 'Media folder', backup: 'Backup folder', recovery: 'Recovery password, if set',
    confirm: 'The previous application is stopped; I will review the queue', telegramId: 'Your numeric Telegram ID', password: 'Panel password',
    repeat: 'Repeat password', setupButton: 'Create installation', preparing: 'Preparing your installation…', login: 'Sign in to Edizora',
    loginIntro: 'Use the Telegram ID and password chosen during setup.', signIn: 'Sign in', signing: 'Signing in…',
    prototype: 'Local application check', note: 'Test profile · your working installation and publishing are not used.',
    signOut: 'Sign out', channels: 'Channels', publications: 'Publications', none: 'Nothing here yet.', refresh: 'Refresh',
    create: 'Add a test channel', alias: 'Short name', title: 'Title', chat: 'Telegram chat ID', add: 'Add channel',
    upload: 'Image check', choose: 'Choose an image', uploading: 'Uploading…', cancel: 'Stop waiting', image: 'Uploaded image',
    preparingHost: 'Setup complete. Starting services…', error: 'Could not complete the action', invalid: 'Check the form fields.',
    closeNote: 'Closing the window keeps the application running in the tray. Use “Quit and stop” to stop it.',
    trayTitle: 'Edizora will keep running', trayHelp: 'The window will hide to the tray and the queue will keep running on this computer.',
    trayHide: 'Hide to tray', trayCancel: 'Keep window open',
    exit: 'Quit and stop', stopping: 'Stopping Edizora', stoppingHelp: 'Waiting for current operations to finish and services to stop safely.',
    stopFailed: 'Could not confirm shutdown', stopFailedHelp: 'Check the application state and try again. No process was forcibly stopped.',
    retryStop: 'Try stopping again', returnApp: 'Return to the app',
  },
} as const;

function message(response: Response): string {
  const error = response.body?.error;
  if (typeof error === 'object' && error && 'message' in error && typeof error.message === 'string') return error.message;
  return typeof response.body?.message === 'string' ? response.body.message : `HTTP ${response.status}`;
}

setApiTransport(new DesktopTransport(invoke));
async function request(path: string, method = 'GET', body?: unknown): Promise<Record<string, unknown>> {
  return (await api<Record<string, unknown> | null>(path, { method, body })) || {};
}

function App() {
  const [language, setLanguage] = useState<Language>('ru');
  const t = words[language];
  const [phase, setPhase] = useState<Phase>('starting');
  const [elapsed, setElapsed] = useState(0);
  const [startupNotice, setStartupNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [signedIn, setSignedIn] = useState(false);
  const [mode, setMode] = useState('new');
  const [setupDone, setSetupDone] = useState(false);
  const [channels, setChannels] = useState<Entry[]>([]);
  const [publications, setPublications] = useState<Entry[]>([]);
  const [media, setMedia] = useState('');
  const [uploading, setUploading] = useState(false);
  const [shutdown, setShutdown] = useState<ShutdownPhase>('idle');
  const [closing, setClosing] = useState(false);
  const [progress, setProgress] = useState(0);
  const uploadId = useRef('');
  const authVersion = useRef(0);
  const shutdownRef = useRef<ShutdownPhase>('idle');
  const hideButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const startupStarted = useRef(Date.now());

  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  useEffect(() => { shutdownRef.current = shutdown; if (shutdown !== 'idle') setClosing(false); }, [shutdown]);
  useEffect(() => { if (closing) hideButton.current?.focus(); }, [closing]);

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;
    void listen('desktop-disconnected', () => {
      authVersion.current += 1;
      clearSession();
      setSignedIn(false); setChannels([]); setPublications([]); setMedia(''); setPhase('failed');
    }).then(cleanup => { if (cancelled) cleanup(); else unsubscribe = cleanup; });
    return () => { cancelled = true; unsubscribe?.(); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;
    void listen('desktop-close-request', () => {
      if (shutdownRef.current === 'stopping') return;
      try {
        if (localStorage.getItem('edizora-prototype-tray-notice') === 'seen') {
          void invoke('desktop_hide').catch(reason => setError(String(reason)));
          return;
        }
      } catch { /* Show the explanation when preferences are unavailable. */ }
      previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setClosing(true);
    }).then(cleanup => { if (cancelled) cleanup(); else unsubscribe = cleanup; });
    return () => { cancelled = true; unsubscribe?.(); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;
    const readStatus = () => {
      void invoke<{ phase: ShutdownPhase }>('desktop_shutdown_status').then(status => {
        if (!cancelled) setShutdown(status.phase);
      }).catch(() => { if (!cancelled) setShutdown('failed'); });
    };
    void listen<ShutdownPhase>('desktop-shutdown', event => setShutdown(event.payload)).then(cleanup => {
      if (cancelled) cleanup(); else unsubscribe = cleanup;
    });
    const onVisibility = () => { if (!document.hidden) readStatus(); };
    document.addEventListener('visibilitychange', onVisibility);
    readStatus();
    return () => { cancelled = true; unsubscribe?.(); document.removeEventListener('visibilitychange', onVisibility); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let timer: number | undefined;
    let nativeVisible = true;
    let visibilityVersion = 0;
    let unsubscribe: (() => void) | undefined;
    const poll = async () => {
      if (cancelled || inFlight || !nativeVisible || document.hidden) return;
      inFlight = true;
      let nextPhase: Phase = 'failed';
      const observedAuthVersion = authVersion.current;
      try {
        const status = await invoke<{ phase: Phase; signed_in: boolean }>('desktop_status');
        nextPhase = status.phase;
        if (cancelled || observedAuthVersion !== authVersion.current) return;
        setPhase(status.phase);
        if (!status.signed_in) {
          clearSession();
          setSignedIn(false); setChannels([]); setPublications([]); setMedia('');
        }
        setElapsed(Date.now() - startupStarted.current);
      } catch {
        if (!cancelled && observedAuthVersion === authVersion.current) setPhase('failed');
      } finally {
        inFlight = false;
        // WebView2 does not necessarily update document.hidden when its
        // native window hides, so both visibility signals are needed.
        if (!cancelled && nativeVisible && !document.hidden) {
          timer = window.setTimeout(() => void poll(), nextPhase === 'starting' || nextPhase === 'setup' ? 500 : 5000);
        }
      }
    };
    const setNativeVisibility = (visible: boolean) => {
      visibilityVersion += 1;
      if (nativeVisible === visible) return;
      nativeVisible = visible;
      window.clearTimeout(timer);
      if (visible && !document.hidden) void poll();
    };
    void listen<boolean>('desktop-visibility', event => setNativeVisibility(event.payload)).then(cleanup => {
      if (cancelled) { cleanup(); return; }
      unsubscribe = cleanup;
      const observedVersion = visibilityVersion;
      void invoke<boolean>('desktop_is_visible').then(visible => {
        if (!cancelled && observedVersion === visibilityVersion) setNativeVisibility(visible);
      }).catch(() => { /* Future native visibility events still apply. */ });
    });
    const onVisibility = () => {
      window.clearTimeout(timer);
      if (!document.hidden && nativeVisible) void poll();
    };
    document.addEventListener('visibilitychange', onVisibility);
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      unsubscribe?.();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  useEffect(() => {
    let unsubscribe: (() => void) | undefined;
    let cancelled = false;
    void listen<{ uploaded: number; total: number }>('desktop-upload-progress', event => {
      if (event.payload.total) setProgress(Math.min(100, Math.round(event.payload.uploaded / event.payload.total * 100)));
    }).then(cleanup => { if (cancelled) cleanup(); else unsubscribe = cleanup; });
    return () => { cancelled = true; unsubscribe?.(); };
  }, []);

  useEffect(() => () => { if (media) void invoke('desktop_revoke_media', { url: media }); }, [media]);

  const refresh = useCallback(async () => {
    try {
      const [channelData, publicationData] = await Promise.all([request('/channels'), request('/publications?limit=20')]);
      setChannels(Array.isArray(channelData.items) ? channelData.items as Entry[] : []);
      setPublications(Array.isArray(publicationData.items) ? publicationData.items as Entry[] : []);
      setError('');
    } catch (reason) { setError(String(reason)); }
  }, []);

  async function setup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const value = (name: string) => String(data.get(name) || '');
    setBusy(true); setError('');
    try {
      const response = await invoke<Response>('desktop_setup', { input: {
        admin_id: Number(value('admin')), password: value('password'), password_repeat: value('repeat'), mode,
        checkout: value('checkout'), database: value('database'), storage: value('storage'),
        confirmed_stopped: data.has('confirmed'), backup: value('backup'), recovery_password: value('recovery'),
      } });
      if (response.status >= 400) throw new Error(message(response));
      setSetupDone(true);
      form.reset();
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true); setError('');
    try {
      const session = await api<Session>('/auth/password', { method: 'POST', body: { telegram_id: Number(data.get('admin')), password: data.get('password') } });
      setSession(session);
      authVersion.current += 1;
      setSignedIn(true); void refresh();
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function logout() {
    authVersion.current += 1;
    try { await request('/auth/logout', 'POST', {}); }
    catch (reason) { setError(String(reason)); }
    finally { clearSession(); setSignedIn(false); setChannels([]); setPublications([]); setMedia(''); }
  }

  async function createChannel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true); setError('');
    try {
      await request('/channels', 'POST', { alias: data.get('alias'), title: data.get('title'), telegram_chat_id: data.get('chat') });
      form.reset(); await refresh();
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function upload() {
    setError('');
    try {
      const file = await invoke<FileHandle | null>('desktop_pick_file');
      if (!file) return;
      setUploading(true); setProgress(0); uploadId.current = crypto.randomUUID();
      const response = await invoke<Response>('desktop_upload', { id: file.id, requestId: uploadId.current });
      if (response.status >= 400) throw new Error(message(response));
      if (typeof response.body?.url !== 'string') throw new Error(t.invalid);
      setMedia(await invoke<string>('desktop_media', { path: response.body.url }));
    } catch (reason) { setError(String(reason)); }
    finally { uploadId.current = ''; setUploading(false); }
  }

  function stopApplication() {
    setShutdown('stopping');
    void invoke('desktop_shutdown').catch(() => setShutdown('failed'));
  }

  async function retryStartup() {
    setStartupNotice('');
    const observedAuthVersion = authVersion.current;
    try {
      const status = await invoke<{phase: Phase}>('desktop_status');
      if (observedAuthVersion !== authVersion.current) return;
      if (status.phase !== 'failed') {
        setPhase(status.phase);
        if (status.phase === 'starting' || status.phase === 'setup') setStartupNotice(t.stillStarting);
        return;
      }
      await invoke('desktop_retry');
      if (observedAuthVersion !== authVersion.current) return;
      startupStarted.current = Date.now();
      setElapsed(0); setPhase('starting');
    } catch (reason) {
      setStartupNotice(String(reason).includes('startup_unconfirmed') ? t.retryUnconfirmed : t.retryFailed);
    }
  }

  function dismissTrayNotice() {
    setClosing(false);
    window.requestAnimationFrame(() => previousFocus.current?.focus());
  }

  function hideToTray() {
    try { localStorage.setItem('edizora-prototype-tray-notice', 'seen'); } catch { /* The notice can be shown again. */ }
    setClosing(false);
    void invoke('desktop_hide').catch(reason => setError(String(reason)));
  }

  const header = <><header className="proto-header"><div className="proto-brand"><span>▦</span> Edizora <small>{t.badge}</small></div>
    <div className="proto-header-actions"><button className="quiet" onClick={() => setLanguage(language === 'ru' ? 'en' : 'ru')}>{t.language}</button>
      <button className="quiet" onClick={stopApplication} disabled={shutdown === 'stopping'}>{t.exit}</button></div></header>
    {closing && <div className="proto-overlay"><section className="proto-card" role="dialog" aria-modal="true" aria-labelledby="tray-title"
      onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); dismissTrayNotice(); }
        if (event.key === 'Tab' && event.shiftKey && document.activeElement === hideButton.current) { event.preventDefault(); cancelButton.current?.focus(); }
        if (event.key === 'Tab' && !event.shiftKey && document.activeElement === cancelButton.current) { event.preventDefault(); hideButton.current?.focus(); }
      }}><h1 id="tray-title">{t.trayTitle}</h1><p>{t.trayHelp}</p><div className="proto-header-actions">
        <button ref={hideButton} onClick={hideToTray}>{t.trayHide}</button><button ref={cancelButton} className="quiet" onClick={dismissTrayNotice}>{t.trayCancel}</button>
      </div></section></div>}</>;
  const errorBlock = error && <div className="proto-error" role="alert"><strong>{t.error}</strong><p>{error}</p></div>;
  if (shutdown !== 'idle') return <>{header}<main className="proto-center"><section className="proto-card" role={shutdown === 'failed' ? 'alert' : 'status'}>
    <h1>{shutdown === 'failed' ? t.stopFailed : t.stopping}</h1><p>{shutdown === 'failed' ? t.stopFailedHelp : t.stoppingHelp}</p>
    {shutdown === 'failed' && <div className="proto-header-actions"><button onClick={stopApplication}>{t.retryStop}</button>
      <button className="quiet" onClick={() => setShutdown('idle')}>{t.returnApp}</button></div>}</section></main></>;
  if (phase === 'starting' || phase === 'failed' || (phase === 'setup' && setupDone)) return <>{header}<main className="proto-center">
    <section className="proto-card"><div className="proto-mark">▦</div><h1>{phase === 'failed' ? t.failed : setupDone ? t.preparingHost : t.boot}</h1>
      <p>{phase === 'failed' ? t.failedHelp : elapsed > 30000 ? t.slow : t.wait}</p>
      {startupNotice && <p className="proto-error" role="alert">{startupNotice}</p>}
      {(phase === 'failed' || elapsed > 30000) && <button onClick={() => void retryStartup()}>{t.retry}</button>}
    </section></main></>;
  if (phase === 'setup') return <>{header}<main className="proto-center"><section className="proto-card"><h1>{t.setup}</h1><p>{t.intro}</p>{errorBlock}
    <form onSubmit={event => void setup(event)}><label>{t.mode}<select value={mode} onChange={event => setMode(event.target.value)}>
      <option value="new">{t.new}</option><option value="legacy">{t.legacy}</option><option value="restore">{t.restore}</option></select></label>
      {mode === 'legacy' && <><label>{t.checkout}<input name="checkout" required /></label><label>{t.database}<input name="database" required /></label><label>{t.storage}<input name="storage" required /></label></>}
      {mode === 'restore' && <><label>{t.backup}<input name="backup" required /></label><label>{t.recovery}<input name="recovery" type="password" autoComplete="off" /></label></>}
      {mode !== 'new' && <label className="proto-check"><input type="checkbox" name="confirmed" required />{t.confirm}</label>}
      <label>{t.telegramId}<input name="admin" inputMode="numeric" pattern="[1-9][0-9]*" required /></label>
      <label>{t.password}<input name="password" type="password" minLength={12} maxLength={1024} required autoComplete="new-password" /></label>
      <label>{t.repeat}<input name="repeat" type="password" minLength={12} maxLength={1024} required autoComplete="new-password" /></label>
      <button disabled={busy}>{busy ? t.preparing : t.setupButton}</button></form></section></main></>;
  if (!signedIn) return <>{header}<main className="proto-center"><section className="proto-card"><h1>{t.login}</h1><p>{t.loginIntro}</p>{errorBlock}
    <form onSubmit={event => void login(event)}><label>{t.telegramId}<input name="admin" inputMode="numeric" pattern="[1-9][0-9]*" required autoComplete="username" /></label>
      <label>{t.password}<input name="password" type="password" required autoComplete="current-password" /></label>
      <button disabled={busy}>{busy ? t.signing : t.signIn}</button></form></section></main></>;
  return <>{header}<main className="proto-workspace"><div className="proto-heading"><div><h1>{t.prototype}</h1><p>{t.note}</p></div>
    <div><button className="quiet" onClick={() => void refresh()}>{t.refresh}</button> <button className="quiet" onClick={() => void logout()}>{t.signOut}</button></div></div>
    {errorBlock}<div className="proto-grid"><section className="proto-card"><h2>{t.channels}</h2>
      {channels.length ? <ul>{channels.map(item => <li key={item.id}>{item.title || item.name || item.id}</li>)}</ul> : <p>{t.none}</p>}
      <h3>{t.create}</h3><form onSubmit={event => void createChannel(event)}><label>{t.alias}<input name="alias" required /></label>
        <label>{t.title}<input name="title" required /></label><label>{t.chat}<input name="chat" required /></label><button disabled={busy}>{t.add}</button></form></section>
    <section className="proto-card"><h2>{t.publications}</h2>{publications.length ? <ul>{publications.map(item => <li key={item.id}>{item.title || `#${item.id}`}</li>)}</ul> : <p>{t.none}</p>}
      <h3>{t.upload}</h3><button onClick={() => void upload()} disabled={uploading}>{uploading ? t.uploading : t.choose}</button>
      {uploading && <><progress max={100} value={progress} aria-label={t.uploading}>{progress}%</progress>
        <button className="quiet" onClick={() => void invoke('desktop_cancel', { requestId: uploadId.current })}>{t.cancel}</button></>}
      {media && <img className="proto-preview" src={media} alt={t.image} />}</section></div><p className="proto-footnote">{t.closeNote}</p></main></>;
}

createRoot(document.getElementById('root')!).render(<App />);
