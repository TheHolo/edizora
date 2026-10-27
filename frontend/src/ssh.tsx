import { t } from './i18n';
import { useState } from 'react';
import { CheckCircle2, Fingerprint, KeyRound, ShieldCheck } from 'lucide-react';
import { api } from './api';
import { useCommand } from './hooks';
import { ErrorBlock } from './ui';

interface FingerprintRecord {algorithm:string;fingerprint:string;}
interface CheckResult {status:string;message?:string;fingerprints?:FingerprintRecord[];target?:{node_id?:string;profile?:string;role?:string;operating_system?:string;provisioning_required?:boolean};node_id?:string;id?:string;trusted?:boolean;}
export function SSHAccess({profile,onReady}:{profile:{id:string;name?:string};onReady:(id:string,nodeID?:string)=>void}) {
  const [method,setMethod]=useState('key');const [password,setPassword]=useState('');const [key,setKey]=useState('');const [keyFilename,setKeyFilename]=useState('');const [keyPassphrase,setKeyPassphrase]=useState('');const [sudoPassword,setSudoPassword]=useState('');
  const [credentialsPresent,setCredentialsPresent]=useState(false);const [check,setCheck]=useState<CheckResult|null>(null);const [fingerprint,setFingerprint]=useState('');const [confirmed,setConfirmed]=useState(false);
  const command=useCommand();
  async function credentials(){
    await command.run(()=>api('/deployment/profiles/'+profile.id+'/credentials',{method:'POST',body:{password:method==='password'?password:undefined,private_key:method==='key'?key:undefined,private_key_passphrase:method==='key'?keyPassphrase||undefined:undefined,sudo_password:sudoPassword||undefined}}),()=>{
      setCredentialsPresent(true);setPassword('');setKey('');setKeyFilename('');setKeyPassphrase('');setSudoPassword('');
    });
  }
  async function inspect(){await command.run(()=>api<CheckResult>('/deployment/profiles/'+profile.id+'/check',{method:'POST',body:{}}),r=>{setCheck(r);setConfirmed(false);setFingerprint('');if(r.status==='checked')onReady(profile.id,r.node_id||r.target?.node_id);});}
  async function trust(){await command.run(()=>api<CheckResult>('/deployment/profiles/'+profile.id+'/trust',{method:'POST',body:{confirmed_fingerprint:fingerprint}}),r=>{setCheck(r);if(r.status==='checked')onReady(profile.id,r.node_id||r.target?.node_id);});}
  return <div className="ssh-access"><h3><KeyRound size={18}/>{t("Доступ к «")}{profile.name||t("серверу")}»</h3><p className="muted small">{t("Данные доступа хранятся только в памяти управляющей службы до перезапуска. В SQLite публикаций и в хранилище браузера они не записываются.")}</p><ErrorBlock error={command.error}/>
    <div className="form-grid"><label>{t("Способ входа")}<select value={method} onChange={e=>{setMethod(e.target.value);setPassword('');setKey('');setKeyFilename('');}}><option value="key">{t("Приватный SSH-ключ")}</option><option value="password">{t("SSH-пароль")}</option></select></label>
      {method==='key'?<><label>{t("Файл приватного ключа")}<input type="file" onChange={e=>{const file=e.target.files?.[0];if(file)void file.text().then(value=>{setKey(value);setKeyFilename(file.name);});}}/><span className="field-hint">{keyFilename||t("Файл используется только для защищённого соединения")}</span></label><label>{t("Пароль ключа, если задан")}<input type="password" autoComplete="off" value={keyPassphrase} onChange={e=>setKeyPassphrase(e.target.value)}/></label></>:<label>{t("SSH-пароль")}<input type="password" autoComplete="off" value={password} onChange={e=>setPassword(e.target.value)}/></label>}
      <label>{t("Пароль sudo, если требуется")}<input type="password" autoComplete="off" value={sudoPassword} onChange={e=>setSudoPassword(e.target.value)}/><span className="field-hint">{t("Для фиксированных установочных действий. Доступ с sudo без пароля можно оставить пустым.")}</span></label>
    </div><div className="button-row"><button onClick={()=>void credentials()} disabled={command.busy||(method==='key'?!key:!password)}><KeyRound size={16}/>{t("Передать доступ")}</button><button onClick={()=>void inspect()} disabled={command.busy}><ShieldCheck size={16}/>{t("Проверить соединение")}</button>{credentialsPresent&&<span className="status success"><CheckCircle2 size={14}/>{t("Доступ принят в память")}</span>}</div>
    {check&&<div className={'notice '+(check.status==='checked'?'success':'amber')}><div><strong>{check.status==='checked'?t("Назначение проверено"):t("Требуется проверка доверия")}</strong><p>{check.message||(check.status==='checked'?t("ОС, роль, доступ и свободное место проверены. Можно составить план."):t("Сопоставьте fingerprint с ключом в консоли сервера или у вашего администратора."))}</p>
      {!!check.fingerprints?.length&&<><label>{t("Ключ сервера")}<select value={fingerprint} onChange={e=>{setFingerprint(e.target.value);setConfirmed(false);}}><option value="">{t("Выберите проверенный ключ")}</option>{check.fingerprints.map(item=><option key={item.fingerprint} value={item.fingerprint}>{item.algorithm} · {item.fingerprint}</option>)}</select></label>{fingerprint&&<p className="mono">{fingerprint}</p>}<label className="checkbox-label"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>{t("Я сверил fingerprint с доверенным источником")}</label><button disabled={command.busy||!fingerprint||!confirmed} onClick={()=>void trust()}><Fingerprint size={17}/>{t("Подтвердить ключ и проверить сервер")}</button></>}
      {check.target&&<dl className="details-list"><dt>{t("ОС / профиль")}</dt><dd>{check.target.operating_system||check.target.profile||'—'}</dd><dt>{t("Роль")}</dt><dd>{check.target.role||'—'}</dd><dt>{t("Установка")}</dt><dd>{check.target.provisioning_required?t("Будет подготовлена после остановки публикаций"):t("Установлена")}</dd></dl>}
    </div></div>}
  </div>;
}
