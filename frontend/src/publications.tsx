import { t } from './i18n';
import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowDown, ArrowLeft, ArrowUp, CalendarDays, Check, Clock3, FileImage, Grid2X2, History, ImagePlus, List, Loader2, Plus, Save, Search, Send, Sparkles, Trash2, Upload, X } from 'lucide-react';
import { api, ApiError, safeURL, upload } from './api';
import { useCommand, useDebounce, useQuery } from './hooks';
import { Link, setUnsaved, useNavigate } from './navigation';
import { date, Empty, ErrorBlock, Heading, Loading, OperationCard, Panel, PublicationIdentity, SafeRichText, Status, Thumbnail, zonedCandidates } from './ui';
import type { Asset, Channel, CommandResult, EventRecord, ID, Operation, Page, Preview, Publication } from './types';

export interface PageProps { channels: Channel[]; channelID: string; timezone: string; }
const tabs = [{label:t("Все"),value:''},{label:t("Черновики"),value:'waiting_confirmation'},{label:t("В очереди"),value:'queue'},{label:t("История"),value:'history'}];
export function Publications({ channels, channelID, timezone }: PageProps) {
  const initial = new URLSearchParams(location.search);
  const [search,setSearch] = useState(initial.get('search') || '');
  const debounced = useDebounce(search,300);
  const [status,setStatus] = useState(initial.get('status') || '');
  const [kind,setKind] = useState(initial.get('content_kind') || '');
  const [from,setFrom] = useState(initial.get('date_from') || '');
  const [until,setUntil] = useState(initial.get('date_to') || '');
  const [cursor,setCursor] = useState('');
  const [cursorStack,setCursorStack] = useState<string[]>([]);
  const [selected,setSelected] = useState<Map<ID,number>>(new Map());
  const [bulkReport,setBulkReport] = useState<Record<string,unknown> | null>(null);
  const [view,setView] = useState(() => localStorage.getItem('publisher-view') === 'gallery' ? 'gallery' : 'table');
  const params = new URLSearchParams({limit:'50'});
  if(debounced) params.set('search',debounced);
  if(status) params.set('status',status);
  if(kind) params.set('content_kind',kind);
  if(channelID) params.set('channel_id',channelID);
  if(cursor) params.set('cursor',cursor);
  if(from) params.set('date_from',from);
  if(until) params.set('date_to',until);
  const query = useQuery<Page<Publication>>('/publications?'+params,10000);
  const operations = useQuery<Page<Operation>>('/operations?limit=5&active=true',10000);
  const command = useCommand();
  const [deletionNotice,setDeletionNotice]=useState('');
  useEffect(() => {
    setSelected(new Map()); setCursor(''); setCursorStack([]);
    const p = new URLSearchParams();
    if(debounced) p.set('search',debounced);
    if(status) p.set('status',status);
    if(kind) p.set('content_kind',kind);
    if(from) p.set('date_from',from);
    if(until) p.set('date_to',until);
    history.replaceState(null,'','/publications'+(p.size?'?'+p:''));
  },[debounced,status,kind,channelID,from,until]);
  function choose(id: ID, version: number) {
    setSelected(previous => {const next=new Map(previous); if(next.has(id)) next.delete(id); else next.set(id,version); return next;});
  }
  function changeView(next: string) { setView(next); localStorage.setItem('publisher-view',next); }
  async function remove(item:Publication){
    if(!confirm(t("Удалить «{0}» навсегда? Запись, история и неиспользуемые медиа будут удалены из приложения. Сообщения в Telegram останутся. Отменить удаление нельзя.",[item.title])))return;
    await command.run(()=>api<{cleanup_pending:boolean}>('/publications/'+item.id,{method:'DELETE',body:{expected_version:item.version}}),result=>{
      setDeletionNotice(result.cleanup_pending?t("Запись удалена. Занятые файлы будут удалены автоматически после освобождения."):t("Запись и неиспользуемые медиа удалены."));
      setSelected(previous=>{const next=new Map(previous);next.delete(item.id);return next;});query.reload();operations.reload();
    });
  }
  async function bulk(action: string) {
    if(action==='delete'&&!confirm(t("Удалить выбранные публикации ({0}) навсегда? Записи, история и неиспользуемые медиа будут удалены. Сообщения в Telegram останутся. Отменить удаление нельзя.",[selected.size])))return;
    if(action === 'cancel' && !confirm(t("Отменить выбранные публикации (")+selected.size+t(")? Уже отправленные сообщения останутся в канале."))) return;
    await command.run(() => api<Record<string,unknown>>('/queue/bulk',{method:'POST',body:{action,items:[...selected].map(([id,expected_version]) => ({id,expected_version})),expected_selection_count:selected.size}}), report => {setBulkReport(report);setSelected(new Map());query.reload();});
  }
  const items = query.data?.items || [];
  const activeOperations = (operations.data?.items||[]).filter(operation=>!['succeeded','cancelled','failed'].includes(operation.status));
  const intakeOperations = (operations.data?.items||[]).filter(operation=>['import','news'].includes(operation.kind)&&!['succeeded','cancelled'].includes(operation.status)&&!operation.result?.publication_id);
  return <>
    <Heading title={t("Публикации")} subtitle={t("Все материалы в одном месте")} action={<Link className="button primary" href="/publications/new"><Plus size={19}/>{t("Новая публикация")}</Link>}/>
    <div className="workspace-with-activity"><div className="primary-workspace">
      <div className="tabs" role="tablist" aria-label={t("Статус публикаций")}>{tabs.map(tab=><button role="tab" aria-selected={status===tab.value} className={status===tab.value?'active':''} key={tab.label} onClick={()=>setStatus(tab.value)}>{tab.label}</button>)}</div>
      <div className="filterbar">
        <label className="search"><Search size={18}/><span className="sr-only">{t("Поиск публикаций")}</span><input value={search} onChange={e=>setSearch(e.target.value)} placeholder={t("Найти публикацию…")}/></label>
        <label><span className="sr-only">{t("Тип материала")}</span><select value={kind} onChange={e=>setKind(e.target.value)}><option value="">{t("Все типы")}</option><option value="artwork">{t("Изображения")}</option><option value="news">{t("Новости")}</option><option value="text">{t("Текст")}</option></select></label>
        <div className="segmented"><button className={view==='table'?'active':''} onClick={()=>changeView('table')} aria-label={t("Таблица")} aria-pressed={view==='table'}><List size={18}/></button><button className={view==='gallery'?'active':''} onClick={()=>changeView('gallery')} aria-label={t("Галерея")} aria-pressed={view==='gallery'}><Grid2X2 size={18}/></button></div>
        <details className="date-filter"><summary>{t("Период")}</summary><div className="popover"><label>{t("С даты")}<input type="date" value={from} onChange={e=>setFrom(e.target.value)}/></label><label>{t("По дату")}<input type="date" value={until} onChange={e=>setUntil(e.target.value)}/></label></div></details>
      </div>
      <ErrorBlock error={query.error} retry={query.reload}/><ErrorBlock error={command.error}/>{deletionNotice&&<p className="notice" role="status">{deletionNotice}</p>}
      {intakeOperations.length>0&&<section className="intake-operations" aria-label={t("Материалы в подготовке")}>{intakeOperations.slice(0,5).map(operation=><div className="intake-row" key={operation.id}><Sparkles size={18}/><div><strong>{operation.kind==='news'?t("Готовится новость"):t("Импорт изображений")}</strong><p className="muted small">{operation.message||t("Сервер принял задачу")}</p></div><Status value={operation.status}/><Link href={'/operations/'+operation.id} className="button secondary">{t("Подробнее")}</Link></div>)}</section>}
      {selected.size>0 && <div className="selection-toolbar"><strong>{t("Выбрано: ")}{selected.size}</strong><button disabled={command.busy} onClick={()=>void bulk('enqueue')}>{t("В очередь")}</button><button disabled={command.busy} onClick={()=>void bulk('retry')}>{t("Повторить ошибки")}</button><button disabled={command.busy} onClick={()=>void bulk('cancel')}>{t("Отменить")}</button><button className="danger-text" disabled={command.busy} onClick={()=>void bulk('delete')}><Trash2 size={15}/>{t("Удалить навсегда")}</button><button className="icon-button" aria-label={t("Снять выделение")} onClick={()=>setSelected(new Map())}><X size={17}/></button></div>}
      {bulkReport && <details className="notice" open><summary>{t("Результаты массовой команды")}</summary><ul className="bulk-results">{(Array.isArray(bulkReport.items)?bulkReport.items:[]).map((value:unknown,index:number)=>{const item=value as {id?:ID;status?:string;error?:{message?:string}};return <li key={item.id||index}><span>{t("Публикация № ")}{item.id}</span><span>{{success:t("Выполнено"),conflict:t("Изменилась версия или состояние"),blocked:t("Действие заблокировано"),not_found:t("Не найдена")}[item.status||'']||item.status}</span>{item.error?.message&&<p className="small muted">{item.error.message}</p>}</li>;})}</ul></details>}
      {query.loading && !query.data ? <Loading/> : !items.length ? <Panel><Empty title={t("Здесь появятся ваши материалы")} description={search || status || kind ? t("По выбранным фильтрам ничего не найдено. Попробуйте изменить условия.") : t("Добавьте ссылку, новость или текст. Сначала появится черновик — публикация запускается отдельным действием.")} action={<Link className="button secondary" href="/publications/new"><Plus size={17}/>{t("Создать публикацию")}</Link>}/></Panel> :
      view==='gallery' ? <div className="publication-gallery">{items.map(item=><article className="publication-card" key={item.id}>
        <div className="gallery-image"><Thumbnail src={item.album?.cover} title={item.title}/><label className="gallery-checkbox"><input type="checkbox" checked={selected.has(item.id)} onChange={()=>choose(item.id,item.version)} aria-label={t("Выбрать ")+item.title}/></label>{!!item.album?.total && <span className="media-count"><FileImage size={14}/>{item.album.selected} {t(" кадров")}</span>}</div>
        <div className="gallery-body">{item.allowed_actions?.includes('delete')&&<button className="icon-button danger-text gallery-delete" disabled={command.busy} onClick={()=>void remove(item)} aria-label={t("Удалить публикацию «{0}»",[item.title])}><Trash2 size={16}/></button>}<Link className="title-link" href={'/publications/'+item.id}>{item.title || t("Без названия")}</Link><p className="muted small">{item.channel?.name || t("Канал не выбран")}</p><div className="between"><Status value={item.status} uncertain={typeof item.error==='object' && item.error?.code==='uncertain_publish'}/><span className="small muted">{date(item.schedule?.utc || item.schedule?.estimate,timezone,true)}</span></div></div>
      </article>)}</div> :
      <div className="table-container"><table className="publications-table"><thead><tr><th className="check-cell"><input type="checkbox" aria-label={t("Выбрать все на странице")} checked={!!items.length && items.every(item=>selected.has(item.id))} onChange={e=>setSelected(e.target.checked?new Map(items.map(item=>[item.id,item.version])):new Map())}/></th><th>{t("Материал")}</th><th className="channel-column">{t("Канал")}</th><th>{t("Статус")}</th><th className="schedule-column">{t("Публикация")}</th><th><span className="sr-only">{t("Открыть")}</span></th></tr></thead><tbody>{items.map(item=><tr key={item.id}>
        <td className="check-cell"><input type="checkbox" checked={selected.has(item.id)} onChange={()=>choose(item.id,item.version)} aria-label={t("Выбрать ")+item.title}/></td>
        <td><PublicationIdentity item={item}/>{item.error && <span className="inline-error small">{typeof item.error==='string'?item.error:item.error.message}</span>}</td>
        <td className="channel-column"><span className="channel-pill" title={item.channel?.name || channels.find(c=>c.id===item.channel_id)?.name || t("Не выбран")}><Send size={13}/><span>{item.channel?.name || channels.find(c=>c.id===item.channel_id)?.name || t("Не выбран")}</span></span></td>
        <td><Status value={item.status} uncertain={typeof item.error==='object' && item.error?.code==='uncertain_publish'}/></td>
        <td className="schedule-column"><span className="small">{date(item.schedule?.utc || item.schedule?.estimate,timezone,true)}</span>{item.schedule?.kind==='estimate' && <span className="muted tiny">{t("Прогноз")}</span>}</td>
        <td><Link href={'/publications/'+item.id} className="icon-button" aria-label={t("Открыть ")+item.title}>···</Link>{item.allowed_actions?.includes('delete')&&<button className="icon-button danger-text" disabled={command.busy} onClick={()=>void remove(item)} aria-label={t("Удалить публикацию «{0}»",[item.title])}><Trash2 size={16}/></button>}</td>
      </tr>)}</tbody></table></div>}
      <div className="pagination"><span className="muted small">{query.data ? t("Показано ")+items.length+(query.data.total!==undefined?t(" из ")+query.data.total:'') : ''}</span><div><button disabled={!cursorStack.length} onClick={()=>{const stack=[...cursorStack];setCursor(stack.pop()||'');setCursorStack(stack);}}>{t("Назад")}</button><button disabled={!query.data?.next_cursor} onClick={()=>{setCursorStack(v=>[...v,cursor]);setCursor(query.data?.next_cursor||'');}}>{t("Дальше")}</button></div></div>
    </div><aside className="activity-rail"><h2>{t("Сейчас в работе")}</h2>{activeOperations.length ? activeOperations.slice(0,3).map(operation=><OperationCard operation={operation} compact key={operation.id}/>) : <Panel><div className="quiet-state"><span className="check-circle"><Check size={20}/></span><strong>{t("Всё спокойно")}</strong><p>{t("Принятые операции появятся здесь. Можно продолжать работу с материалами.")}</p></div></Panel>}<div className="rail-note"><Clock3 size={17}/><p>{t("Точное время и прогноз публикации учитывают часовой пояс ")}<strong>{timezone}</strong>.</p></div></aside></div>
  </>;
}

export function CreatePublication({channels,channelID}:PageProps) {
  const [kind,setKind]=useState('artwork'); const [source,setSource]=useState(''); const [body,setBody]=useState(''); const [title,setTitle]=useState('');
  const [channel,setChannel]=useState(channelID || String(channels.find(c=>c.is_default)?.id || channels[0]?.id || '')); const [tags,setTags]=useState('');
  const [assets,setAssets]=useState<string[]>([]); const [filenames,setFilenames]=useState<string[]>([]); const [uploading,setUploading]=useState(false); const [uploadError,setUploadError]=useState<unknown>(null);
  const command=useCommand(); const navigate=useNavigate();
  useEffect(()=>{
    const dirty=!!(source.trim()||body.trim()||title.trim()||assets.length);
    setUnsaved(dirty);
    const warn=(event:BeforeUnloadEvent)=>{if(dirty){event.preventDefault();event.returnValue='';}};
    window.addEventListener('beforeunload',warn);
    return()=>{setUnsaved(false);window.removeEventListener('beforeunload',warn);};
  },[source,body,title,assets]);
  async function submit(e:FormEvent) {
    e.preventDefault();
    const payload={content_kind:kind,title,body,urls:source.split(/\n+/).map(v=>v.trim()).filter(Boolean),source_url:source.trim().split('\n')[0] || undefined,channel_id:Number(channel),tags:tags.split(/[\s,]+/).filter(Boolean),asset_ids:assets};
    await command.run(()=>api<CommandResult>(kind==='news'?'/publications/news':kind==='text'?'/publications/text-draft':'/publications/import',{method:'POST',body:payload}),r=>{setUnsaved(false);navigate(r.operation_id?'/operations/'+r.operation_id:'/publications/'+(r.publication_id || r.job_id || r.id));});
  }
  async function addFiles(files:FileList|null) {
    if(!files)return;setUploading(true);setUploadError(null);
    try{for(const file of Array.from(files)){const asset=await upload(file);setAssets(v=>[...v,asset.id]);setFilenames(v=>[...v,file.name]);}}
    catch(e){setUploadError(e);}finally{setUploading(false);}
  }
  return <>
    <Heading title={t("Новая публикация")} subtitle={t("Подготовим материал и сохраним его как черновик")} eyebrow={<Link href="/publications"><ArrowLeft size={14}/>{t("Публикации")}</Link>}/>
    <form className="create-layout" onSubmit={e=>void submit(e)}><Panel title={t("Материал")}>
      <div className="tabs create-tabs" role="tablist" aria-label={t("Тип материала")}>{[{id:'artwork',label:t("Изображения"),Icon:FileImage},{id:'news',label:t("Новость"),Icon:Sparkles},{id:'text',label:t("Текст"),Icon:List}].map(({id,label,Icon})=><button type="button" key={id} role="tab" aria-selected={kind===id} className={kind===id?'active':''} onClick={()=>setKind(id)}><Icon size={17}/>{label}</button>)}</div>
      {kind!=='text' && <label>{kind==='artwork'?t("Ссылки на изображения"):t("Ссылка на источник")}<textarea rows={kind==='artwork'?5:2} value={source} onChange={e=>setSource(e.target.value)} placeholder={kind==='artwork'?t("Ссылка Pixiv, DeviantArt или прямое изображение\nКаждая ссылка — с новой строки"):t("Публичная статья, YouTube с субтитрами или Telegram-пост")}/><span className="field-hint">{kind==='artwork'?t("Для каждой ссылки будет отдельный результат."):t("Можно вместо ссылки вставить исходный текст ниже. Закрытые каналы и видео без субтитров недоступны.")}</span></label>}
      <label>{t("Название")}{kind==='text'?'':t(" (необязательно)")}<input value={title} onChange={e=>setTitle(e.target.value)} placeholder={t("Как назвать материал")} required={kind==='text'}/></label>
      {kind!=='artwork' && <label>{kind==='news'?t("Исходный текст"):t("Текст публикации")}<textarea value={body} onChange={e=>setBody(e.target.value)} rows={10} placeholder={kind==='news'?t("Вставьте материал для обработки домашней моделью…"):t("Напишите текст…")} required={kind==='text'}/></label>}
      <label className="upload-zone"><Upload size={24}/><strong>{uploading?t("Загружаем файлы…"):t("Добавить файлы")}</strong><span>{t("Изображения и медиа — с проверкой на сервере")}</span><input type="file" multiple accept="image/*,video/*" disabled={uploading} onChange={e=>void addFiles(e.target.files)}/></label>
      {filenames.length>0 && <ul className="uploaded-files">{filenames.map((name,i)=><li key={name+'-'+i}><FileImage size={16}/><span>{name}</span><button type="button" className="icon-button" aria-label={t("Убрать ")+name} onClick={()=>{setFilenames(v=>v.filter((_,index)=>index!==i));setAssets(v=>v.filter((_,index)=>index!==i));}}><X size={15}/></button></li>)}</ul>}
      <ErrorBlock error={uploadError}/>
    </Panel><div><Panel title={t("Параметры публикации")}>
      <label>{t("Канал")}<select value={channel} onChange={e=>setChannel(e.target.value)} required><option value="" disabled>{t("Выберите канал")}</option>{channels.filter(c=>!c.archived).map(c=><option key={c.id} value={String(c.id)}>{c.name}{c.enabled===false?t(" · выключен"):''}</option>)}</select></label>
      <label>{t("Пользовательские теги")}<input value={tags} onChange={e=>setTags(e.target.value)} placeholder="#art #night"/><span className="field-hint">{t("Через пробел или запятую")}</span></label>
      {kind==='news' && <div className="notice violet"><Sparkles size={19}/><p>{t("AI обработает материал на зарегистрированном исполнителе. Если домашний компьютер выключен, задача дождётся его возвращения.")}</p></div>}
      <ErrorBlock error={command.error}/>
      <button className="button primary full" type="submit" disabled={command.busy||uploading||!channel||(!source.trim()&&!body.trim()&&!assets.length)}>{command.busy?<Loader2 size={18} className="spin"/>:<Plus size={18}/>}{t("Создать черновик")}</button>
    </Panel><div className="rail-note"><Save size={18}/><p>{t("Принятая задача продолжится после закрытия вкладки. Локальный компьютер с Edizora должен оставаться включённым.")}</p></div></div></form>
  </>;
}

interface Draft { title:string; title_translation:string; description:string; body:string; caption_override:string; user_tags:string; channel_id:string; }
function draftFrom(job:Publication):Draft{return {title:job.title||'',title_translation:job.title_translation||'',description:job.description||'',body:job.body||job.text||'',caption_override:job.caption_override||job.manual_caption||'',user_tags:(job.user_tags||job.tags||[]).join(' '),channel_id:String(job.channel?.id||job.channel_id||'')};}

export function Editor({id,channels,timezone}:PageProps & {id:string}) {
  const query=useQuery<Publication>('/publications/'+id,2000);
  const preview=useQuery<Preview>(query.data?'/publications/'+id+'/preview?version='+query.data.version:null,2000);
  const preparationOperation=useQuery<Operation>(query.data?.last_operation_id&&!query.data.readiness?.ready?'/operations/'+query.data.last_operation_id:null,2000);
  const [draft,setDraft]=useState<Draft|null>(null); const [saved,setSaved]=useState<Draft|null>(null);
  const [assets,setAssets]=useState<Asset[]>([]); const [activeTab,setActiveTab]=useState('fields');
  const [saveState,setSaveState]=useState('saved'); const [saveError,setSaveError]=useState<unknown>(null); const [conflict,setConflict]=useState(false);
  const [onlySelected,setOnlySelected]=useState(false); const [numbers,setNumbers]=useState('');
  const [dragID,setDragID]=useState<ID|null>(null);
  const [scheduleOpen,setScheduleOpen]=useState(false); const [scheduleInput,setScheduleInput]=useState(''); const [chosenInstant,setChosenInstant]=useState('');
  const [historyOpen,setHistoryOpen]=useState(false); const [resolveOpen,setResolveOpen]=useState(false);
  const [resolution,setResolution]=useState<'published'|'not_published'|'partial'|'still_unknown'>('still_unknown'); const [note,setNote]=useState(''); const [knownIDs,setKnownIDs]=useState('');
  const [assetError,setAssetError]=useState<unknown>(null); const [uploading,setUploading]=useState(false);
  const command=useCommand();const navigate=useNavigate();const loaded=useRef('');
  const saving=useRef<Promise<Publication|undefined>|null>(null);
  const deleting=useRef(false);
  const currentJob=useRef<Publication|null>(null);const draftRef=useRef(draft);draftRef.current=draft;const savedRef=useRef(saved);savedRef.current=saved;
  currentJob.current=query.data;
  const historyQuery=useQuery<{events:EventRecord[];publications:{id:ID;message_ids:number[];published_at:string}[]}>(historyOpen?'/publications/'+id+'/history':null);
  const dirty=!!draft&&!!saved&&JSON.stringify(draft)!==JSON.stringify(saved);
  const preparationFailed=!query.data?.readiness?.ready&&preparationOperation.data?.id===query.data?.last_operation_id&&preparationOperation.data?.kind==='prepare'&&['failed','waiting_user','cancelled'].includes(preparationOperation.data.status);
  const preparationReasons=query.data?.readiness?.reasons?.filter(reason=>!['Медиа ещё не подготовлены на активном узле','Требуется подготовить предпросмотр'].includes(reason))||[];
  const readinessMessage=preparationReasons.length?preparationReasons.join(' · '):preparationFailed?t("Не удалось обновить предпросмотр"):t("Изображения и предпросмотр обновляются автоматически…");
  const editable=query.data?.status==='waiting_confirmation';
  const captionEditable=['waiting_confirmation','queued','scheduled'].includes(query.data?.status||'');
  useEffect(()=>{
    if(query.data&&loaded.current!==id){loaded.current=id;setDraft(draftFrom(query.data));setSaved(draftFrom(query.data));setAssets(query.data.assets||query.data.album?.items||[]);}
  },[query.data,id]);
  useEffect(()=>{
    // Background downloads update thumbnails without replacing local selection or fields.
    if(!query.data?.assets)return;
    const fresh=new Map(query.data.assets.map(asset=>[asset.id,asset]));
    setAssets(previous=>previous.map(asset=>{const updated=fresh.get(asset.id);return updated?{...asset,state:updated.state,thumbnail_url:updated.thumbnail_url}:asset;}));
  },[query.data?.assets]);
  useEffect(()=>{
    setUnsaved(dirty);
    const warn=(e:BeforeUnloadEvent)=>{if(dirty){e.preventDefault();e.returnValue='';}};
    window.addEventListener('beforeunload',warn);
    return()=>{setUnsaved(false);window.removeEventListener('beforeunload',warn);};
  },[dirty]);
  async function save():Promise<Publication|undefined>{
    if(deleting.current)return;
    if(saving.current){const result=await saving.current;if(!result)return;return save();}
    if(!draftRef.current||!currentJob.current)return;
    if(JSON.stringify(draftRef.current)===JSON.stringify(savedRef.current))return currentJob.current;
    const snapshot={...draftRef.current};const version=currentJob.current.version;
    setSaveState('saving');setSaveError(null);
    const pending=(async()=>{
      try{
        const changes:Record<string,unknown>={expected_version:version};
        for(const key of Object.keys(snapshot) as (keyof Draft)[]){if(snapshot[key]!==savedRef.current?.[key])changes[key]=key==='user_tags'?snapshot.user_tags.split(/[\s,]+/).filter(Boolean):key==='channel_id'?Number(snapshot.channel_id):snapshot[key];}
        const result=await api<Publication>('/publications/'+id,{method:'PATCH',body:changes});
        const serverDraft=draftFrom(result);const nextDraft={...draftRef.current!};
        for(const key of Object.keys(snapshot) as (keyof Draft)[]){if(nextDraft[key]===snapshot[key])nextDraft[key]=serverDraft[key];}
        query.setData(result);currentJob.current=result;draftRef.current=nextDraft;savedRef.current=serverDraft;setDraft(nextDraft);setSaved(serverDraft);
        const hasNewEdits=JSON.stringify(nextDraft)!==JSON.stringify(serverDraft);setSaveState(hasNewEdits?'dirty':'saved');setUnsaved(hasNewEdits);
        return result;
      }catch(error){
        setSaveError(error);const isConflict=error instanceof ApiError&&error.status===409;
        setConflict(isConflict);setSaveState(isConflict?'conflict':'offline');return undefined;
      }finally{saving.current=null;}
    })();saving.current=pending;const result=await pending;
    if(result&&JSON.stringify(draftRef.current)!==JSON.stringify(savedRef.current))return save();
    return result;
  }
  useEffect(()=>{
    if(!dirty||conflict)return;
    setSaveState('dirty');const timer=setTimeout(()=>void save(),850);return()=>clearTimeout(timer);
  },[draft,saved,conflict]);
  function field<K extends keyof Draft>(key:K,value:Draft[K]){setDraft(previous=>previous?{...previous,[key]:value}:previous);}
  async function action(name:string,extra:Record<string,unknown>={}){
    const job=await save();if(!job)return;
    if(name==='publish-now'&&!confirm(t("Опубликовать «")+job.title+t("» сейчас в канале «")+(job.channel?.name || t("не выбран"))+t("»? Версия ")+job.version+t(". Это действие начнёт реальную отправку.")))return;
    if(name==='cancel'&&!confirm(t("Отменить публикацию? Если отправка уже началась, будет запрошена безопасная остановка. Отправленные сообщения останутся в канале.")))return;
    await command.run(()=>api<CommandResult>('/publications/'+id+'/actions/'+name,{method:'POST',body:{expected_version:job.version,...extra}}),result=>{
      if(result.operation_id)navigate('/operations/'+result.operation_id);else {query.reload();setScheduleOpen(false);setResolveOpen(false);}
    });
  }
  async function removePublication(){
    const job=currentJob.current;if(!job||saving.current)return;
    if(!confirm(t("Удалить «{0}» навсегда? Запись, история и неиспользуемые медиа будут удалены из приложения. Сообщения в Telegram останутся. Отменить удаление нельзя.",[job.title])))return;
    deleting.current=true;
    try{await command.run(()=>api<{cleanup_pending:boolean;cleanup_operation_id:string}>('/publications/'+id,{method:'DELETE',body:{expected_version:job.version}}),result=>{
      setUnsaved(false);draftRef.current=savedRef.current;
      if(result.cleanup_pending)navigate('/operations/'+result.cleanup_operation_id);else navigate('/publications');
    });}finally{deleting.current=false;}
  }
  async function preparation(path:string){
    const job=await save();if(!job)return;
    await command.run(()=>api<CommandResult>('/publications/'+id+'/'+path,{method:'POST',body:{expected_version:job.version}}),r=>r.operation_id?navigate('/operations/'+r.operation_id):query.reload());
  }
  async function saveAssets(next:Asset[]){
    const job=await save();if(!job)return;setAssetError(null);
    await command.run(()=>api<{items:Asset[];version:number}>('/publications/'+id+'/album',{method:'PUT',body:{expected_version:job.version,selected_asset_ids:next.filter(a=>a.selected).sort((a,b)=>(a.order??a.source_index)-(b.order??b.source_index)).map(a=>a.id)}}),r=>{
      setAssets(r.items||next);query.setData({...job,version:r.version,assets:r.items||next});query.reload();
    });
  }
  function toggleAsset(asset:Asset){
    const selected=assets.filter(a=>a.selected);
    void saveAssets(assets.map(a=>a.id===asset.id?{...a,selected:!a.selected,order:!a.selected?selected.length:a.order}:a));
  }
  function moveAsset(asset:Asset,direction:number){
    const selected=assets.filter(a=>a.selected).sort((a,b)=>(a.order??a.source_index)-(b.order??b.source_index));
    const index=selected.findIndex(a=>a.id===asset.id);const target=index+direction;if(target<0||target>=selected.length)return;
    [selected[index],selected[target]]=[selected[target],selected[index]];
    const order=new Map(selected.map((a,i)=>[a.id,i]));
    void saveAssets(assets.map(a=>({...a,order:order.get(a.id)??a.order})));
  }
  function dropAsset(target:Asset){
    if(dragID===null||!target.selected||target.id===dragID)return;
    const selected=assets.filter(a=>a.selected).sort((a,b)=>(a.order??a.source_index)-(b.order??b.source_index));
    const source=selected.findIndex(a=>a.id===dragID);const destination=selected.findIndex(a=>a.id===target.id);
    if(source<0||destination<0)return;
    selected.splice(destination,0,selected.splice(source,1)[0]);
    const order=new Map(selected.map((a,i)=>[a.id,i]));
    setDragID(null);void saveAssets(assets.map(a=>({...a,order:order.get(a.id)??a.order})));
  }
  async function replaceFiles(files:FileList|null){
    if(!files)return;const job=await save();if(!job)return;setUploading(true);setAssetError(null);
    try{
      const uploaded=[];for(const file of Array.from(files))uploaded.push((await upload(file)).id);
      const result=await api<Publication>('/publications/'+id,{method:'PATCH',body:{expected_version:job.version,asset_ids:uploaded}});
      query.setData(result);setAssets(result.assets||[]);
    }catch(e){setAssetError(e);}finally{setUploading(false);}
  }
  async function applyLocalToLatest(){
    await command.run(()=>api<Publication>('/publications/'+id),fresh=>{
      query.setData(fresh);currentJob.current=fresh;setSaved(draftFrom(fresh));setConflict(false);setSaveError(null);setSaveState('dirty');
    });
  }
  const job=query.data;
  const allowed=(name:string)=>!!job?.allowed_actions?.includes(name);
  const selectedAssets=assets.filter(a=>a.selected).sort((a,b)=>(a.order??a.source_index)-(b.order??b.source_index));
  const candidates=scheduleInput?zonedCandidates(scheduleInput,timezone):[];
  if(query.loading&&!job)return <Loading/>;
  if(!job||!draft)return <><Heading title={t("Редактор публикации")}/><ErrorBlock error={query.error} retry={query.reload}/></>;
  return <>
    <Heading title={job.title||t("Без названия")} eyebrow={<Link href="/publications">{t("Публикации / ")}{job.status==='waiting_confirmation'?t("Черновик"):'№ '+job.id}</Link>} action={<><span className={'save-indicator '+saveState} aria-live="polite">{saveState==='saving'?<Loader2 size={15} className="spin"/>:saveState==='saved'&&!dirty?<Check size={15}/>:<Save size={15}/>}{{saved:dirty?t("Есть изменения"):t("Все изменения сохранены"),dirty:t("Есть изменения"),saving:t("Сохраняем…"),offline:t("Нет связи · не сохранено"),conflict:t("Конфликт версий")}[saveState]}</span><Link href="/publications" className="button secondary"><X size={16}/>{t("Закрыть")}</Link></>}/>
    <ErrorBlock error={query.error}/><ErrorBlock error={saveError}/>
    {conflict&&<div className="notice amber"><div><strong>{t("Ваш текст остался в редакторе.")}</strong><p>{t("Можно загрузить серверный вариант или явно применить ваши поля к актуальной версии.")}</p><div className="button-row"><button onClick={()=>{
      if(confirm(t("Заменить введённые поля текущим вариантом сервера?")))void command.run(()=>api<Publication>('/publications/'+id),fresh=>{query.setData(fresh);setDraft(draftFrom(fresh));setSaved(draftFrom(fresh));setConflict(false);setSaveError(null);setSaveState('saved');});
    }}>{t("Загрузить серверный вариант")}</button><button onClick={()=>void applyLocalToLatest()}>{t("Применить мои поля")}</button></div></div></div>}
    {preparationFailed&&<div className="notice amber"><p>{t("Не удалось загрузить изображения или обновить предпросмотр. Проверьте источник и повторите загрузку.")} <Link href={'/operations/'+preparationOperation.data!.id}>{t("Открыть подробности")}</Link></p><button onClick={()=>void preparation('prepare')} disabled={command.busy}>{t("Повторить загрузку")}</button></div>}
    <div className="editor-summary"><Status value={job.status} uncertain={typeof job.error==='object'&&job.error?.code==='uncertain_publish'}/><span className="small muted">{t("Версия ")}{job.version}</span><span className={'readiness '+(job.readiness?.ready?'ready':'')}>{job.readiness?.ready?<Check size={16}/>:<Clock3 size={16}/>} {job.readiness?.ready?t("Готово к публикации"):readinessMessage}</span></div>
    <div className="mobile-editor-tabs tabs" role="tablist" aria-label={t("Редактор")}>{[{id:'media',label:t("Медиа")},{id:'fields',label:t("Поля")},{id:'preview',label:t("Предпросмотр")}].map(tab=><button key={tab.id} role="tab" aria-selected={activeTab===tab.id} className={activeTab===tab.id?'active':''} onClick={()=>setActiveTab(tab.id)}>{tab.label}</button>)}</div>
    <div className={'editor-grid tab-'+activeTab}>
      <Panel title={t("Изображения")} className="editor-media">
        <p className="muted small">{t("Выбрано ")}{selectedAssets.length} {t(" из ")}{assets.length || job.album?.total || 0}{job.album?.selection_limit!=null?t(" · лимит ")+job.album.selection_limit:''}</p>
        {editable&&assets.length>0&&<>
          <div className="album-toolbar">
            <button disabled={command.busy||(job.album?.selection_limit!=null&&assets.length>job.album.selection_limit)} title={job.album?.selection_limit!=null&&assets.length>job.album.selection_limit?t("Каталог больше сохранённого лимита. Выберите первые допустимые кадры."):undefined} onClick={()=>void saveAssets(assets.map(a=>({...a,selected:true,order:a.source_index})))}>{t("Выбрать все")}</button>
            <button disabled={command.busy} onClick={()=>{const ids=assets.slice().sort((a,b)=>a.source_index-b.source_index).slice(0,job.album?.selection_limit??assets.length).map(a=>a.id);void saveAssets(assets.map(a=>({...a,selected:ids.includes(a.id),order:ids.indexOf(a.id)})));}}>{t("Первые допустимые")}</button>
            <button disabled={command.busy||!job.album?.initial_selected_asset_ids} onClick={()=>{const ids=job.album?.initial_selected_asset_ids||[];void saveAssets(assets.map(a=>({...a,selected:ids.includes(a.id),order:ids.indexOf(a.id)})));}}>{t("Исходный выбор")}</button>
            <button disabled={command.busy} onClick={()=>void saveAssets(assets.map(a=>({...a,selected:false})))}>{t("Очистить")}</button><label className="checkbox-label"><input type="checkbox" checked={onlySelected} onChange={e=>setOnlySelected(e.target.checked)}/>{t("Только выбранные")}</label>
          </div>
          <div className="inline-form"><label><span className="sr-only">{t("Номера кадров")}</span><input value={numbers} onChange={e=>setNumbers(e.target.value)} placeholder={t("Номера: 1, 3, 5–8")}/></label><button disabled={command.busy} onClick={()=>{
            const indices:number[]=[];
            for(const chunk of numbers.split(/[ ,]+/)){const range=chunk.match(/^(\d+)[–-](\d+)$/);if(range){for(let i=Number(range[1]);i<=Math.min(Number(range[2]),assets.length);i++)indices.push(i);}else if(/^\d+$/.test(chunk))indices.push(Number(chunk));}
            void saveAssets(assets.map(a=>({...a,selected:indices.includes(a.source_index),order:indices.indexOf(a.source_index)})));
          }}>{t("Применить")}</button></div>
        </>}
        <div className="album-grid">{assets.filter(a=>!onlySelected||a.selected).sort((a,b)=>a.selected===b.selected?(a.selected?(a.order??a.source_index)-(b.order??b.source_index):a.source_index-b.source_index):a.selected?-1:1).map(asset=><article className={'album-item '+(asset.selected?'selected':'')} key={asset.id} draggable={editable&&asset.selected&&!command.busy} onDragStart={event=>{setDragID(asset.id);event.dataTransfer.setData('text/plain',String(asset.id));event.dataTransfer.effectAllowed='move';}} onDragOver={event=>{if(editable&&asset.selected&&!command.busy)event.preventDefault();}} onDrop={event=>{event.preventDefault();if(editable&&!command.busy)dropAsset(asset);}} onDragEnd={()=>setDragID(null)}>
          <Thumbnail src={asset.thumbnail_url} title={t("Кадр ")+(asset.source_index)}/>
          <label className="album-selection"><input type="checkbox" checked={asset.selected} disabled={!editable||command.busy} onChange={()=>toggleAsset(asset)}/><span>{asset.source_index}</span></label>
          {asset.selected&&selectedAssets[0]?.id===asset.id&&<span className="cover-label">{t("Первый")}</span>}
          {asset.state==='missing'&&<span className="asset-missing">{t("Оригинал недоступен")}</span>}
          {asset.selected&&editable&&<div className="album-order">
            <button className="icon-button" aria-label={t("Переместить кадр ")+(asset.source_index)+t(" раньше")} disabled={command.busy||selectedAssets[0]?.id===asset.id} onClick={()=>moveAsset(asset,-1)}><ArrowUp size={14}/></button>
            <button className="icon-button" aria-label={t("Переместить кадр ")+(asset.source_index)+t(" позже")} disabled={command.busy||selectedAssets.at(-1)?.id===asset.id} onClick={()=>moveAsset(asset,1)}><ArrowDown size={14}/></button>
            <button onClick={()=>moveAsset(asset,-selectedAssets.findIndex(a=>a.id===asset.id))} disabled={command.busy||selectedAssets[0]?.id===asset.id}>{t("Первым")}</button>
          </div>}
        </article>)}</div>
        {!assets.length&&<Empty title={job.content_kind==='text'?t("Текст без изображений"):t("Медиа пока нет")} description={job.content_kind==='text'?t("Можно добавить изображения или опубликовать только текст."):t("Подготовьте материал или добавьте файлы.")}/>}
        {editable&&<label className="button secondary full file-button"><ImagePlus size={17}/>{uploading?t("Загружаем…"):t("Заменить медиа")}<input type="file" accept="image/*,video/*" multiple disabled={uploading} onChange={e=>void replaceFiles(e.target.files)}/></label>}
        <ErrorBlock error={assetError}/>
      </Panel>
      <Panel title={t("Основная информация")} className="editor-fields">
        <label>{t("Название")}<input value={draft.title} disabled={!editable} onChange={e=>field('title',e.target.value)}/></label>
        <label>{t("Перевод названия")}<input value={draft.title_translation} disabled={!editable} onChange={e=>field('title_translation',e.target.value)} placeholder={t("Ручной или подтверждённый перевод")}/></label>
        <label>{t("Описание")}<textarea value={draft.description} disabled={!editable} rows={3} onChange={e=>field('description',e.target.value)}/></label>
        {['news','text'].includes(job.content_kind)&&<label>{t("Текст")}<textarea value={draft.body} disabled={!editable} rows={8} onChange={e=>field('body',e.target.value)}/></label>}
        <label>{t("Канал")}<select value={draft.channel_id} disabled={!editable} onChange={e=>field('channel_id',e.target.value)}><option value="">{t("Не выбран")}</option>{channels.map(c=><option key={c.id} value={String(c.id)}>{c.name}</option>)}</select></label>
        <label>{t("Пользовательские теги")}<input value={draft.user_tags} disabled={!captionEditable} onChange={e=>field('user_tags',e.target.value)} placeholder="#art #night"/></label>
        <label>{t("Ручная подпись")}<textarea value={draft.caption_override} disabled={!captionEditable} rows={3} onChange={e=>field('caption_override',e.target.value)} placeholder={t("Оставьте пустым для автоматического оформления")}/></label>
        <details><summary>{t("Источник и автор")}</summary><dl className="details-list"><dt>{t("Источник")}</dt><dd>{safeURL(job.source?.url)?<a href={safeURL(job.source?.url)} target="_blank" rel="noreferrer">{job.source?.url}</a>:'—'}</dd><dt>{t("Автор")}</dt><dd>{job.source?.author||'—'}</dd><dt>{t("Обновлено")}</dt><dd>{date(job.updated_at,timezone)}</dd></dl></details>
        <div className="button-row"><button onClick={()=>void preparation('translate-title')} disabled={command.busy||!editable}>{t("Перевести название")}</button>{allowed('reimport')&&<button onClick={()=>void preparation('reimport')} disabled={command.busy||!editable}>{t("Обновить каталог")}</button>}<button onClick={()=>void save()} disabled={!dirty||saveState==='saving'}><Save size={16}/>{t("Сохранить")}</button></div>
        <button className="text-button" onClick={()=>setHistoryOpen(v=>!v)}><History size={15}/>{historyOpen?t("Скрыть историю"):t("История изменений")}</button>
        {historyOpen&&<><ErrorBlock error={historyQuery.error}/><ul className="event-list">{historyQuery.data?.events?.map((event,i)=><li key={event.seq||i}><time>{date(event.created_at||event.timestamp,timezone,true)}</time><span>{event.message||event.action||event.status}</span></li>)}</ul>{historyQuery.data?.publications?.map(publication=><p className="small muted" key={publication.id}>{t("Отправлено: ")}{date(publication.published_at,timezone)} {t(" · ID сообщений: ")}{publication.message_ids.join(', ')}</p>)}</>}
      </Panel>
      <Panel title={t("Предпросмотр в Telegram")} className="editor-preview">
        <ErrorBlock error={preview.error} retry={preview.reload}/>
        {preview.loading&&!preview.data?<Loading/>:<>
          <div className="telegram-preview"><header><span className="channel-avatar"><Send size={17}/></span><div><strong>{job.channel?.name||t("Канал")}</strong><span className="tiny muted">{preview.data?.media_count||selectedAssets.length} {t(" медиа")}</span></div></header>
          {selectedAssets.length>0&&<div className="preview-images">{selectedAssets.slice(0,4).map(asset=><Thumbnail src={asset.thumbnail_url} title={t("Кадр ")+(asset.source_index)} key={asset.id}/>)}</div>}
          {preview.data?.parts?.length?preview.data.parts.map((part,i)=><div className="preview-part" key={i}>{preview.data!.parts!.length>1&&<span className="muted tiny">{t("Сообщение ")}{i+1} · {part.kind==='media'?t("медиа"):t("текст")}</span>}<SafeRichText html={part.html||part.caption_html||part.text||''}/></div>):preview.data?.html?<SafeRichText html={preview.data.html}/>:<p className="preview-text">{preview.data?.text||preview.data?.caption||t("Предпросмотр появится после подготовки материала.")}</p>}
          </div>
          {preview.data?.character_count!==undefined&&<p className="muted small align-right">{t("Символов: ")}{preview.data.character_count}{preview.data.character_limit?' / '+preview.data.character_limit:''}</p>}
          {preview.data?.warnings?.map(warning=><p className="notice amber" key={warning}>{warning}</p>)}
          <p className="muted tiny">{preview.data?.assets_prepared?t("Подготовленный план версии ")+(preview.data?.job_version||preview.data?.version||job.version):t("Предпросмотр обновится автоматически после загрузки изображений.")} {t(" Перед действием изменения сохранятся.")}</p>
        </>}
      </Panel>
    </div>
    <ErrorBlock error={command.error}/>
    {scheduleOpen&&<Panel title={t("Точное время публикации")}><p className="muted">{t("Не раньше выбранного момента · ")}{timezone}</p><div className="inline-form"><label>{t("Дата и время")}<input type="datetime-local" value={scheduleInput} onChange={e=>{setScheduleInput(e.target.value);setChosenInstant('');}}/></label>{candidates.length>1&&<label>{t("Уточните момент")}<select value={chosenInstant} onChange={e=>setChosenInstant(e.target.value)}><option value="">{t("Время встречается дважды")}</option>{candidates.map(candidate=><option value={candidate} key={candidate}>{candidate} UTC</option>)}</select></label>}<button className="primary" disabled={command.busy||!candidates.length||(candidates.length>1&&!chosenInstant)} onClick={()=>void action('schedule',{scheduled_at:chosenInstant||candidates[0],timezone})}>{t("Запланировать")}</button><button onClick={()=>setScheduleOpen(false)}>{t("Закрыть")}</button></div>{scheduleInput&&!candidates.length&&<p className="inline-error">{t("Такого местного времени нет в выбранном часовом поясе. Выберите другое.")}</p>}</Panel>}
    {resolveOpen&&<Panel title={t("Ручная проверка результата")}><p>{t("Проверьте канал и известные сообщения. Это действие записывает ваше заключение и само ничего не отправляет.")}</p><label>{t("Результат")}<select value={resolution} onChange={e=>setResolution(e.target.value as typeof resolution)}><option value="still_unknown">{t("Пока неизвестно")}</option><option value="published">{t("Отправлено")}</option><option value="not_published">{t("Точно не отправлено")}</option><option value="partial">{t("Отправлена только часть")}</option></select></label><label>{t("Известные ID сообщений")}<input value={knownIDs} onChange={e=>setKnownIDs(e.target.value)} placeholder={t("Через пробел")}/></label><label>{t("Примечание")}<textarea value={note} onChange={e=>setNote(e.target.value)} rows={2}/></label><button onClick={()=>void action('resolve-uncertain',{outcome:resolution,note,message_ids:knownIDs.split(/[\s,]+/).filter(Boolean).map(Number)} satisfies Omit<import('./types').API['UncertainResolution'],'expected_version'>)} disabled={command.busy}>{t("Сохранить заключение")}</button></Panel>}
    <footer className="editor-footer"><span className="muted small">{!job.readiness?.ready?readinessMessage:t("Черновик готов к следующему шагу")}</span><div className="button-row">
      {allowed('delete')&&<button className="danger-text" onClick={()=>void removePublication()} disabled={command.busy||saveState==='saving'}><Trash2 size={16}/>{t("Удалить навсегда")}</button>}
      {allowed('return-to-draft')&&<button onClick={()=>void action('return-to-draft')} disabled={command.busy}>{t("Вернуть в черновик")}</button>}
      {allowed('resolve-uncertain')&&<button onClick={()=>setResolveOpen(true)}>{t("Проверить результат")}</button>}
      {allowed('confirm-duplicate')&&<button onClick={()=>{if(confirm(t("Разрешить сознательный повтор этого источника в выбранном канале? Само подтверждение ничего не отправляет.")))void action('confirm-duplicate');}} disabled={command.busy}>{t("Разрешить повтор источника")}</button>}
      {allowed('retry')&&<button onClick={()=>void action('retry')} disabled={command.busy}>{t("Повторить")}</button>}
      {allowed('cancel')&&<button onClick={()=>void action('cancel')} disabled={command.busy}>{t("Отменить")}</button>}
      <button disabled={command.busy||!allowed('preview-in-telegram')} onClick={()=>void preparation('preview-in-telegram')} title={!allowed('preview-in-telegram')?t("Отправка предпросмотра сейчас недоступна"):undefined}><Send size={16}/>{t("Показать в Telegram")}</button>
      <button onClick={()=>setScheduleOpen(v=>!v)} disabled={command.busy||!allowed('schedule')}><CalendarDays size={17}/>{t("Точное время")}</button>
      <button onClick={()=>void action('publish-now')} disabled={command.busy||!allowed('publish-now')} title={job.blocked_reason}>{t("Опубликовать сейчас")}</button>
      <button className="primary" onClick={()=>void action('enqueue')} disabled={command.busy||conflict||!allowed('enqueue')} title={job.blocked_reason}>{command.busy?<Loader2 size={17} className="spin"/>:<Clock3 size={17}/>}{t("В очередь")}</button>
    </div></footer>
  </>;
}
