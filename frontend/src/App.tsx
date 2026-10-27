import { t } from './i18n';
import { version as appVersion } from '../package.json';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { Activity, ArrowRight, CalendarDays, ChevronDown, Cloud, FileText, KeyRound, LayoutGrid, List, Loader2, LogOut, Menu, MoreHorizontal, Send, Settings, ShieldCheck, Sparkles, X } from 'lucide-react';
import { api, ApiError, clearSession, setSession } from './api';
import { useCommand, useQuery } from './hooks';
import { canNavigate, Link, NavigationContext } from './navigation';
import { CreatePublication, Editor, Publications } from './publications';
import { AI, Calendar, Channels, OperationPage, Queue } from './management';
import { Backups, DeploymentPage } from './deployment';
import { SettingsPage, SystemPage } from './settings';
import { DesktopPage } from './desktop';
import type { DesktopStatus } from './desktop';
import { setupTelegram } from './telegram';
import { Empty, ErrorBlock, Loading, Panel, Status } from './ui';
import type { Channel, Control, Deployment, Page, Session } from './types';

const navigation=[{path:'/publications',label:t("Публикации"),Icon:FileText},{path:'/queue',label:t("Очередь"),Icon:List},{path:'/calendar',label:t("Календарь"),Icon:CalendarDays},{path:'/ai',label:t("AI-задачи"),Icon:Sparkles},{path:'/channels',label:t("Каналы"),Icon:Send},{path:'/deployment',label:t("Размещение"),Icon:Cloud},{path:'/settings',label:t("Настройки"),Icon:Settings}];
function Logo(){return <div className="brand"><span className="brand-mark"><i/><i/><i/><i/></span><strong>Edizora</strong><span className="brand-version">{appVersion}</span></div>;}
function Login({onSession,error:initialError}:{onSession:(session:Session)=>void;error?:unknown}){
  const [telegramID,setTelegramID]=useState('');const [password,setPassword]=useState('');const command=useCommand();
  async function submit(e:FormEvent){e.preventDefault();await command.run(()=>api<Session>('/auth/password',{method:'POST',body:{telegram_id:Number(telegramID),password}}),onSession);}
  return <main className="login-page"><div className="login-brand"><Logo/><span className="eyebrow">{t("ВАШЕ МЕСТО ДЛЯ ПУБЛИКАЦИЙ")}</span></div><Panel><div className="login-symbol"><KeyRound size={27}/></div><h1>{t("С возвращением")}</h1><p className="muted">{t("Войдите в панель управления публикациями.")}</p><ErrorBlock error={command.error||initialError}/><form onSubmit={e=>void submit(e)}><label>{t("Telegram ID администратора")}<input inputMode="numeric" autoComplete="username" value={telegramID} onChange={e=>setTelegramID(e.target.value)} required/></label><label>{t("Пароль")}<input type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} required/></label><button className="primary full" disabled={command.busy}>{command.busy?<Loader2 size={17} className="spin"/>:<ArrowRight size={17}/>}{t("Войти")}</button></form><details><summary>{t("Запускаете локально или впервые?")}</summary><p className="muted small">{t("Откройте Edizora через установленный ярлык: он создаст одноразовый код входа. Для первой настройки внешнего пароля используйте доверенный launcher или SSH-команду установки. Открытой регистрации нет.")}</p></details></Panel><p className="login-footer"><ShieldCheck size={15}/>{t("Доступ только для разрешённых администраторов")}</p></main>;
}
function Reauth({close}:{close:()=>void}){
  const [password,setPassword]=useState('');const command=useCommand();
  return <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="reauth-title"><button className="modal-close icon-button" onClick={close} aria-label={t("Закрыть")}><X size={19}/></button><KeyRound size={25}/><h2 id="reauth-title">{t("Подтвердите личность")}</h2><p className="muted">{t("Для изменения размещения и секретов требуется свежий вход. После подтверждения повторите выбранное действие.")}</p><ErrorBlock error={command.error}/><form onSubmit={e=>{e.preventDefault();void command.run(()=>api('/auth/reauth',{method:'POST',body:{password}}),close);}}><label>{t("Пароль администратора")}<input type="password" autoComplete="current-password" autoFocus value={password} onChange={e=>setPassword(e.target.value)} required/></label><button className="primary full" disabled={command.busy}>{t("Подтвердить")}</button></form></section></div>;
}
export default function App(){
  const [session,setCurrentSession]=useState<Session|null>(null);const [booting,setBooting]=useState(true);const [authError,setAuthError]=useState<unknown>(null);
  const [route,setRoute]=useState(location.pathname+location.search);const [channelID,setChannelID]=useState('');const [more,setMore]=useState(false);const [reauth,setReauth]=useState(false);
  const currentRoute=useRef(route);currentRoute.current=route;
  const navigationDepth=useRef(0);
  const navigate=useCallback((path:string)=>{if(!canNavigate())return;history.pushState(null,'',path);navigationDepth.current++;setRoute(path);setMore(false);window.scrollTo({top:0});},[]);
  function acceptSession(value:Session){setSession(value);setCurrentSession(value);setAuthError(null);setBooting(false);if(['/','/login','/setup'].includes(location.pathname)){history.replaceState(null,'','/publications');setRoute('/publications');}}
  useEffect(()=>{
    const cleanup=setupTelegram();let active=true;
    async function boot(){
      try{
        const fragment=new URLSearchParams(location.hash.slice(1));const code=fragment.get('code');
        if(code){history.replaceState(null,'',location.pathname+location.search);const value=await api<Session>('/auth/local-exchange',{method:'POST',body:{code}});if(active)acceptSession(value);return;}
        try{const value=await api<Session>('/auth/session');if(active)acceptSession(value);return;}catch(error){if(!(error instanceof ApiError)||error.status!==401)throw error;}
        const app=window.Telegram?.WebApp;
        if(app?.initData){
          let nonce=sessionStorage.getItem('publisher-exchange-nonce');
          const nonceAt=Number(sessionStorage.getItem('publisher-exchange-time')||0);
          if(!nonce||Date.now()-nonceAt>60000){nonce=crypto.randomUUID();sessionStorage.setItem('publisher-exchange-nonce',nonce);sessionStorage.setItem('publisher-exchange-time',String(Date.now()));}
          const value=await api<Session>('/auth/telegram',{method:'POST',body:{init_data:app.initData,exchange_nonce:nonce}});if(active)acceptSession(value);
        }
      }catch(error){if(active)setAuthError(error);}finally{if(active)setBooting(false);}
    }
    void boot();
    const pop=()=>{if(canNavigate()){navigationDepth.current=Math.max(0,navigationDepth.current-1);setRoute(location.pathname+location.search);}else history.pushState(null,'',currentRoute.current);};
    const needAuth=()=>{clearSession();setCurrentSession(null);};
    const needReauth=()=>setReauth(true);
    window.addEventListener('popstate',pop);window.addEventListener('publisher-auth-needed',needAuth);window.addEventListener('publisher-reauth-needed',needReauth);
    return()=>{active=false;cleanup();window.removeEventListener('popstate',pop);window.removeEventListener('publisher-auth-needed',needAuth);window.removeEventListener('publisher-reauth-needed',needReauth);};
  },[]);
  const channels=useQuery<Page<Channel>>(session?'/channels':null,10000);
  const settings=useQuery<{timezone?:string}>(session?'/settings':null);
  const desktop=useQuery<DesktopStatus>(session?'/desktop/status':null);
  const desktopOffered=useRef(false);
  useEffect(()=>{if(desktop.data&&!desktop.data.telegram_configured&&!desktopOffered.current){desktopOffered.current=true;if(currentRoute.current.split('?')[0]!=='/settings')navigate('/desktop');}},[desktop.data,navigate]);
  const deployment=useQuery<Deployment>(session?'/deployment/current':null,10000);
  const control=useQuery<Control>(session?'/publication-control':null,10000);
  const timezone=settings.data?.timezone||session?.timezone||'Asia/Vladivostok';
  useEffect(()=>{
    const app=window.Telegram?.WebApp;if(!app?.initData)return;
    const back=()=>{if(navigationDepth.current>0)history.back();else if(route.split('?')[0]!=='/publications')navigate('/publications');};
    if(route.split('?')[0]==='/publications')app.BackButton?.hide();else app.BackButton?.show();
    app.BackButton?.onClick(back);return()=>app.BackButton?.offClick(back);
  },[route,navigate]);
  async function logout(){try{await api('/auth/logout',{method:'POST',body:{}});}finally{clearSession();setCurrentSession(null);}}
  let page:ReactNode=null;const path=route.split('?')[0];const props={channels:channels.data?.items||[],channelID,timezone};
  if(path==='/desktop')page=<DesktopPage/>;
  else if(path==='/publications/new')page=<CreatePublication {...props}/>;
  else if(/^\/publications\/[^/]+$/.test(path))page=<Editor key={path} {...props} id={path.split('/')[2]}/>;
  else if(path==='/queue')page=<Queue {...props}/>;
  else if(path==='/calendar')page=<Calendar {...props}/>;
  else if(path==='/ai')page=<AI timezone={timezone}/>;
  else if(path.startsWith('/channels'))page=<Channels timezone={timezone} onChanged={channels.reload}/>;
  else if(path==='/deployment')page=<DeploymentPage timezone={timezone}/>;
  else if(path.startsWith('/deployment/migrations/'))page=<OperationPage id={path.split('/')[3]} timezone={timezone} migration/>;
  else if(path.startsWith('/operations/'))page=<OperationPage id={path.split('/')[2]} timezone={timezone}/>;
  else if(path==='/backups')page=<Backups timezone={timezone}/>;
  else if(path==='/settings')page=<SettingsPage onChanged={settings.reload}/>;
  else if(path==='/system')page=<SystemPage timezone={timezone}/>;
  else if(path==='/moved')page=<Panel><Empty title={t("Приложение перенесено")} description={t("Эта установка передала управление. Актуальный адрес указан на странице размещения.")} action={<Link href="/deployment" className="button primary">{t("Открыть размещение")}</Link>}/></Panel>;
  else page=<Publications {...props}/>;
  if(booting)return <div className="boot"><Logo/><Loading/></div>;
  if(!session)return <Login onSession={acceptSession} error={authError}/>;
  const blocked=control.data?.publication_allowed===false || !!deployment.data&&deployment.data.role!=='active';
  return <NavigationContext.Provider value={navigate}><div className="app-shell"><a href="#main" className="skip-link">{t("К содержимому")}</a><aside className={'sidebar '+(more?'is-open':'')}><Link href="/publications" className="brand-link"><Logo/></Link><nav aria-label={t("Основная навигация")}>{[...navigation,...(desktop.data?[{path:'/desktop',label:t("Приложение"),Icon:Settings}]:[])].map(({path:href,label,Icon})=><Link className={'nav-item '+(path.startsWith(href)?'active':'')} key={href} href={href}><Icon size={21} strokeWidth={1.6}/><span>{label}</span></Link>)}</nav><div className="sidebar-bottom"><Link href="/system"><Activity size={18}/><div><strong>{deployment.data?.hostname||t("Установка Edizora")}</strong><span>{deployment.data?.role?<Status value={deployment.data.role}/>: t("Проверяем состояние…")}</span></div></Link></div></aside><div className="app-main"><header className="topbar"><button className="mobile-menu icon-button" aria-label={t("Открыть навигацию")} onClick={()=>setMore(v=>!v)}><Menu size={22}/></button><label className="channel-select"><Send size={17}/><span className="sr-only">{t("Канал")}</span><select value={channelID} onChange={e=>setChannelID(e.target.value)}><option value="">{t("Все каналы")}</option>{channels.data?.items.map(channel=><option key={channel.id} value={String(channel.id)}>{channel.name}</option>)}</select><ChevronDown size={14}/></label><div className="topbar-right"><span className="timezone-label"><CalendarDays size={16}/>{timezone}</span><button className="profile-button icon-button" onClick={()=>void logout()} aria-label={t("Выйти")} title={t("Выйти · ")+(session.user?.name||session.user?.telegram_id||t("Администратор"))}><LogOut size={18}/></button></div></header><main id="main" className="main-content"><ErrorBlock error={channels.error} retry={channels.reload}/>{blocked&&<div className="maintenance-banner"><PauseIcon/><span>{t("Публикации ограничены состоянием установки. ")}<Link href="/deployment">{t("Открыть подробности")}</Link></span></div>}{page}</main></div><nav className="mobile-bottom" aria-label={t("Мобильная навигация")}><Link href="/publications" className={path.startsWith('/publications')?'active':''}><FileText size={20}/><span>{t("Публикации")}</span></Link><Link href="/queue" className={path==='/queue'?'active':''}><List size={21}/><span>{t("Очередь")}</span></Link><Link href="/ai" className={path==='/ai'?'active':''}><Sparkles size={20}/><span>{t("Задачи")}</span></Link><button onClick={()=>setMore(v=>!v)} className={more?'active':''}><MoreHorizontal size={22}/><span>{t("Ещё")}</span></button></nav>{more&&<button className="sidebar-backdrop" aria-label={t("Закрыть навигацию")} onClick={()=>setMore(false)}/>}</div>{reauth&&<Reauth close={()=>setReauth(false)}/>}</NavigationContext.Provider>;
}
function PauseIcon(){return <LayoutGrid size={17}/>;}
