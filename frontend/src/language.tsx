import { useState } from 'react';
import { getLanguage, setLanguage, t } from './i18n';
import type { Language } from './i18n';

export function LanguageSetting() {
  const [language, selectLanguage] = useState<Language>(getLanguage);
  const [error, setError] = useState(false);
  return <div><label>{t('Язык интерфейса')}<select value={language} onChange={event => selectLanguage(event.target.value as Language)}>
    <option value="en">English</option><option value="ru">Русский</option>
  </select><span className="field-hint">{t('Язык сохраняется в этом браузере. Применение перезагрузит страницу; сначала сохраните остальные изменения.')}</span></label>
    <button disabled={language === getLanguage()} onClick={() => {
      try { setLanguage(language); location.reload(); }
      catch { setError(true); }
    }}>{t('Применить язык')}</button>
    {error && <p role="alert">{t('Не удалось сохранить язык. Разрешите локальное хранилище в браузере.')}</p>}
  </div>;
}
