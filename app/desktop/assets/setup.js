const token = new URLSearchParams(location.hash.slice(1)).get('setup') || '';
history.replaceState(null, '', '/');
const form = document.getElementById('setup');
const mode = document.getElementById('mode');
const message = document.getElementById('message');
mode.addEventListener('change', () => {document.getElementById('legacy').hidden = mode.value !== 'legacy';document.getElementById('restore').hidden = mode.value !== 'restore';});
if (!token) {message.textContent = 'Откройте мастер через ярлык Publisher. Обновление этой страницы не повторяет запуск.';document.getElementById('submit').disabled = true;}
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('submit');
  const value = id => document.getElementById(id).value;
  if (value('password') !== value('repeat')) {message.textContent = 'Пароли не совпали';return;}
  if ((mode.value === 'legacy' && !document.getElementById('confirmed').checked)||(mode.value === 'restore' && !document.getElementById('confirmed-restore').checked)) {message.textContent = 'Подтвердите остановку старого бота';return;}
  button.disabled = true;message.textContent = 'Готовим программу и хранилище. Не закрывайте эту страницу…';
  try {
    const response = await fetch('/setup-api/initialize', {method:'POST', headers:{'Content-Type':'application/json','X-Setup-Token':token},body:JSON.stringify({admin_id:Number(value('admin')),password:value('password'),password_repeat:value('repeat'),mode:mode.value,checkout:value('checkout'),database:value('database'),storage:value('storage'),confirmed_stopped:mode.value === 'restore'?document.getElementById('confirmed-restore').checked:document.getElementById('confirmed').checked,backup:value('backup'),recovery_password:value('recovery-password')})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Не удалось завершить настройку');
    form.reset();message.textContent = result.message;button.textContent = 'Готово';
  } catch (error) {message.textContent = error.message || 'Нет связи с приложением. Откройте Publisher повторно.';button.disabled = false;}
});
