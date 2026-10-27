import { Fragment, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { AlertCircle, Check, CheckCircle2, ChevronRight, Clock3, FileImage, FileText, Loader2, RefreshCw, ShieldAlert, WifiOff } from 'lucide-react';
import { ApiError, hasBearer, mediaBlob, safeURL } from './api';
import { Link } from './navigation';
import type { Operation, Publication } from './types';

export const statusLabels: Record<string, string> = {
  ready: 'Готово', ok: 'В норме', unchecked: 'Не проверено', leased: 'Выполняется', finalizing: 'Сохраняем результат', created: 'Получен', resolving: 'Получаем источник', waiting_confirmation: 'Черновик', queued: 'В очереди', scheduled: 'Точное время',
  downloading: 'Загружаем', processing: 'Подготовка', publishing: 'Отправляется', completed: 'Опубликовано', failed: 'Ошибка', cancelled: 'Отменено',
  running: 'В работе', succeeded: 'Готово', waiting_external: 'Ожидаем ответ', waiting_worker: 'Ждём домашний компьютер', waiting_user: 'Нужно ваше действие',
  cancelling: 'Останавливаем', active: 'Активен', standby: 'Ожидает активации', retired: 'Передал управление', recovery_required: 'Нужно восстановление',
  online: 'На связи', offline: 'Не на связи', revoked: 'Доступ отозван', pending: 'Ожидает', uncertain_publish: 'Результат неизвестен',
};
export const stageLabels: Record<string, string> = {
  accepted: 'Задание принято', source: 'Получение источника', fetching: 'Получение источника', extracting: 'Извлечение текста', media: 'Подготовка медиа',
  ai: 'Ответ модели', model: 'Ответ модели', drafting: 'Создание черновика', ready: 'Проверка черновика', prepare: 'Подготовка',
  planned: 'План подготовлен', stopping: 'Остановка публикаций', quiesced: 'Проверка остановки', preparing_target: 'Подготовка назначения',
  snapshotting: 'Согласованный снимок', transferring: 'Передача данных', verifying: 'Проверка целостности', retiring_source: 'Передача управления',
  activation_committed: 'Право передано', activating_target: 'Активация назначения', routing: 'Проверка адреса', completed: 'Завершение',
};
export function date(value?: string | number | null, timezone = 'Asia/Vladivostok', short = false) {
  if (!value) return '—';
  const parsed = new Date(typeof value === 'number' ? value * 1000 : value); if (Number.isNaN(parsed.valueOf())) return '—';
  return new Intl.DateTimeFormat('ru-RU', { timeZone: timezone, day: 'numeric', month: short ? 'short' : 'long', hour: '2-digit', minute: '2-digit' }).format(parsed);
}
export function bytes(value?: number) { if (value === undefined) return '—'; const unit = value >= 1073741824 ? 1073741824 : value >= 1048576 ? 1048576 : value >= 1024 ? 1024 : 1; return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value / unit)} ${unit === 1 ? 'Б' : unit === 1024 ? 'КБ' : unit === 1048576 ? 'МБ' : 'ГБ'}`; }
export function Status({ value, uncertain = false }: { value: string; uncertain?: boolean }) {
  const current = uncertain ? 'uncertain_publish' : value;
  const success = ['completed', 'succeeded', 'active', 'online'].includes(current);
  const failure = ['failed', 'uncertain_publish', 'recovery_required', 'revoked'].includes(current);
  const working = ['running', 'processing', 'resolving', 'downloading', 'publishing', 'waiting_worker', 'waiting_external'].includes(current);
  const Icon = success ? CheckCircle2 : failure ? AlertCircle : working ? Loader2 : ['queued', 'scheduled'].includes(current) ? Clock3 : FileText;
  return <span className={`status ${success ? 'success' : failure ? 'danger' : working ? 'violet' : ['queued','scheduled','waiting_user'].includes(current) ? 'amber' : ''}`}><Icon size={14} aria-hidden="true" />{statusLabels[current] || current}</span>;
}
export function ErrorBlock({ error, retry }: { error: unknown; retry?: () => void }) {
  if (!error) return null;
  const data = error instanceof ApiError ? error.data : undefined;
  const Icon = error instanceof ApiError && error.status === 0 ? WifiOff : error instanceof ApiError && error.status === 403 ? ShieldAlert : AlertCircle;
  return <div className="notice error" role="alert"><Icon size={20} /><div><strong>{data?.code === 'version_conflict' ? 'Содержимое изменилось в другом окне' : 'Не удалось завершить действие'}</strong><p>{error instanceof Error ? error.message : String(error)}</p>{data?.field_errors && <ul>{Object.entries(data.field_errors).map(([key,value]) => <li key={key}>{key}: {Array.isArray(value) ? value.join(', ') : value}</li>)}</ul>}{retry && <button className="text-button" onClick={retry}><RefreshCw size={14} />Повторить</button>}</div></div>;
}
export function Empty({ title, description, action, icon = 'image' }: { title: string; description?: string; action?: ReactNode; icon?: 'image' | 'text' }) {
  return <div className="empty">{icon === 'image' ? <FileImage size={38} strokeWidth={1.2} /> : <FileText size={38} strokeWidth={1.2} />}<h3>{title}</h3>{description && <p>{description}</p>}{action}</div>;
}
export function Loading() { return <div className="loading" role="status"><Loader2 size={23} className="spin" /><span>Загружаем данные…</span></div>; }
export function Heading({ title, subtitle, action, eyebrow }: { title: string; subtitle?: string; action?: ReactNode; eyebrow?: ReactNode }) {
  return <div className="page-heading"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>{action && <div className="heading-actions">{action}</div>}</div>;
}
export function Panel({ title, action, children, className = '' }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>{title && <div className="panel-heading"><h2>{title}</h2>{action}</div>}{children}</section>;
}
export function Thumbnail({ src, title, className = '' }: { src?: string | null; title: string; className?: string }) {
  const reference=useRef<HTMLSpanElement>(null);
  const [resolved,setResolved]=useState<string|undefined>(hasBearer()?undefined:safeURL(src));
  const [failed,setFailed]=useState(false);
  useEffect(()=>{
    setFailed(false);
    if(!src||!hasBearer()){setResolved(safeURL(src));return;}
    let objectURL:string|undefined;const controller=new AbortController();
    const observer=new IntersectionObserver(entries=>{
      if(!entries.some(entry=>entry.isIntersecting))return;
      observer.disconnect();
      void mediaBlob(src,controller.signal).then(blob=>{objectURL=URL.createObjectURL(blob);setResolved(objectURL);}).catch(error=>{if(!(error instanceof DOMException&&error.name==='AbortError'))setFailed(true);});
    },{rootMargin:'150px'});
    if(reference.current)observer.observe(reference.current);
    return()=>{observer.disconnect();controller.abort();if(objectURL)URL.revokeObjectURL(objectURL);};
  },[src]);
  return <span ref={reference} className={'thumbnail-holder '+className}>{resolved&&!failed?<img className="thumbnail" src={resolved} alt={title} loading="lazy" onError={()=>setFailed(true)}/>:<span className="thumbnail placeholder" role="img" aria-label="Изображение недоступно"><FileImage size={25} strokeWidth={1.3}/></span>}</span>;
}
export function PublicationIdentity({ item }: { item: Publication }) {
  return <div className="publication-identity"><div className="thumbnail-wrap"><Thumbnail src={item.album?.cover} title={item.title} /></div><div><Link href={`/publications/${item.id}`} className="title-link">{item.title || 'Без названия'}</Link><div className="muted small">{item.source?.provider || ({ text: 'Текст', news: 'Новость', artwork: 'Изображения' }[item.content_kind] || 'Материал')}{item.album?.total ? ` · ${item.album.selected} из ${item.album.total} кадров` : ''}</div></div></div>;
}
export function Stepper({ operation }: { operation: Operation }) {
  const stages = operation.stages?.length ? operation.stages : [{ key: operation.stage_key, status: operation.status }];
  return <ol className="stepper">{stages.map((stage,index) => <li key={stage.key} className={`${stage.status} ${stage.key === operation.stage_key ? 'current' : ''}`}><span className="step-bullet">{stage.status === 'succeeded' ? <Check size={17} /> : stage.status === 'failed' ? <AlertCircle size={17} /> : index + 1}</span><div><strong>{stage.label || stageLabels[stage.key] || stage.key}</strong>{stage.message && <p>{stage.message}</p>}{stage.key === operation.stage_key && operation.message && <p>{operation.message}</p>}</div></li>)}</ol>;
}
export function ProgressBar({ operation }: { operation: Operation }) {
  const p = operation.progress;
  if (!p || !p.total || p.total <= 0) return <p className="muted small">{operation.message || 'Ожидаем следующий этап. Работа выполняется на сервере.'}</p>;
  const percent = Math.max(0, Math.min(100, p.completed / p.total * 100));
  return <div className="measured-progress"><div className="between"><span>{p.unit === 'bytes' ? `${bytes(p.completed)} из ${bytes(p.total)}` : `${p.completed} из ${p.total} ${p.unit === 'files' ? 'файлов' : 'элементов'}`}</span><strong>{Math.round(percent)}%</strong></div><progress value={p.completed} max={p.total} aria-label="Прогресс текущего этапа" />{p.estimated_seconds_remaining != null && <span className="muted small">Прогноз: ещё около {Math.ceil(p.estimated_seconds_remaining / 60)} мин.</span>}</div>;
}
export function OperationCard({ operation, compact = false }: { operation: Operation; compact?: boolean }) {
  return <Panel title={operation.message || stageLabels[operation.stage_key] || 'Текущая операция'}><Status value={operation.status} />{!compact && <Stepper operation={operation} />}<ProgressBar operation={operation} /><Link href={`/operations/${operation.id}`} className="button secondary full">Подробнее<ChevronRight size={16} /></Link></Panel>;
}
export function SafeRichText({ html }: { html: string }) {
  const root = new DOMParser().parseFromString(html, 'text/html').body;
  function render(node: ChildNode, index: number): ReactNode {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (!(node instanceof Element)) return null;
    const children = Array.from(node.childNodes).map(render);
    switch(node.tagName.toLowerCase()) {
      case 'b': case 'strong': return <strong key={index}>{children}</strong>;
      case 'i': case 'em': return <em key={index}>{children}</em>;
      case 'u': return <u key={index}>{children}</u>;
      case 's': case 'del': return <s key={index}>{children}</s>;
      case 'code': return <code key={index}>{children}</code>;
      case 'pre': return <pre key={index}>{children}</pre>;
      case 'br': return <br key={index} />;
      case 'a': { const href = safeURL(node.getAttribute('href')); return href ? <a key={index} href={href} target="_blank" rel="noreferrer noopener">{children}</a> : <Fragment key={index}>{children}</Fragment>; }
      default: return <Fragment key={index}>{children}</Fragment>;
    }
  }
  return <div className="rich-text">{Array.from(root.childNodes).map(render)}</div>;
}
export function utcToInput(value: string | null | undefined, timezone: string) {
  if (!value) return '';
  const parts = new Intl.DateTimeFormat('sv-SE', { timeZone: timezone, year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23' }).format(new Date(value));
  return parts.replace(' ', 'T');
}
export function zonedCandidates(input: string, timezone: string) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(input)) return [];
  const approximate = Date.parse(`${input}Z`); const matches: string[] = [];
  for(let offset = -14 * 60; offset <= 14 * 60; offset += 15) {
    const candidate = new Date(approximate + offset * 60000).toISOString();
    if (utcToInput(candidate,timezone) === input) matches.push(candidate);
  }
  return matches;
}
