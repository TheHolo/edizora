export interface TelegramWebApp {
  initData:string; ready:()=>void; expand:()=>void; setHeaderColor?:(color:string)=>void;setBackgroundColor?:(color:string)=>void;
  viewportHeight?:number;viewportStableHeight?:number;safeAreaInset?:{top:number;bottom:number;left:number;right:number};
  contentSafeAreaInset?:{top:number;bottom:number;left:number;right:number};enableClosingConfirmation?:()=>void;disableClosingConfirmation?:()=>void;
  onEvent?:(event:string,callback:()=>void)=>void;offEvent?:(event:string,callback:()=>void)=>void;
  BackButton?:{show:()=>void;hide:()=>void;onClick:(callback:()=>void)=>void;offClick:(callback:()=>void)=>void};
  openLink?:(url:string)=>void;openTelegramLink?:(url:string)=>void;
}
declare global {interface Window {Telegram?:{WebApp:TelegramWebApp};}}
export function setupTelegram(){
  const app=window.Telegram?.WebApp;if(!app?.initData)return()=>{};
  app.ready();app.expand();app.setHeaderColor?.('#151b1f');app.setBackgroundColor?.('#121619');
  const update=()=>{const root=document.documentElement;root.style.setProperty('--app-height',(app.viewportHeight||window.innerHeight)+'px');root.style.setProperty('--tg-safe-top',(app.contentSafeAreaInset?.top||app.safeAreaInset?.top||0)+'px');root.style.setProperty('--tg-safe-bottom',(app.contentSafeAreaInset?.bottom||app.safeAreaInset?.bottom||0)+'px');};
  update();app.onEvent?.('viewportChanged',update);app.onEvent?.('safeAreaChanged',update);app.onEvent?.('contentSafeAreaChanged',update);
  const external=(event:MouseEvent)=>{
    if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey)return;
    const anchor=event.target instanceof Element?event.target.closest('a[href]'):null;
    if(!anchor)return;
    const url=new URL(anchor.getAttribute('href')||'',location.origin);
    if(url.origin===location.origin||!['https:','http:'].includes(url.protocol))return;
    if(url.hostname==='t.me'&&app.openTelegramLink){event.preventDefault();app.openTelegramLink(url.href);}
    else if(app.openLink){event.preventDefault();app.openLink(url.href);}
  };
  document.addEventListener('click',external);
  return()=>{app.offEvent?.('viewportChanged',update);app.offEvent?.('safeAreaChanged',update);app.offEvent?.('contentSafeAreaChanged',update);document.removeEventListener('click',external);};
}
