import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import { getLanguage, locale, setLanguage, t } from '../src/i18n.ts';
import { english } from '../src/messages.ts';
import { api } from '../src/api.ts';

const values = new Map();
globalThis.localStorage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
const originalFetch = globalThis.fetch;
afterEach(() => { values.clear(); globalThis.fetch = originalFetch; });

test('English is the default, including for invalid preferences and restricted storage', () => {
  assert.equal(getLanguage(), 'en');
  assert.equal(t('Настройки'), 'Settings');
  assert.equal(locale(), 'en-US');
  values.set('edizora-language', 'de');
  assert.equal(getLanguage(), 'en');
  const storage = globalThis.localStorage;
  try {
    globalThis.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
    assert.equal(getLanguage(), 'en');
    assert.throws(() => setLanguage('ru'), /blocked/);
  } finally { globalThis.localStorage = storage; }
});

test('saved Russian and English preferences survive reads and preserve substitutions', () => {
  setLanguage('ru');
  assert.equal(getLanguage(), 'ru');
  assert.equal(locale(), 'ru-RU');
  assert.equal(t('Настройки'), 'Настройки');
  assert.equal(t(' · {0} из {1} кадров', [2, 5]), ' · 2 из 5 кадров');
  setLanguage('en');
  assert.equal(t(' · {0} из {1} кадров', [2, 5]), ' · 2 of 5 frames');
  assert.equal(t('Не удалось выполнить запрос ({0}).', [503]), 'Request failed (503).');
});

test('all interface catalog entries have English text and matching placeholders', () => {
  for (const [source, translation] of Object.entries(english)) {
    assert.ok(translation.length, source);
    assert.doesNotMatch(translation, /[А-Яа-яЁё]/, source);
    assert.deepEqual(translation.match(/\{\d+\}/g) ?? [], source.match(/\{\d+\}/g) ?? [], source);
  }
});

test('every literal translation call is covered by the interface catalog', () => {
  for (const file of fs.readdirSync(new URL('../src/', import.meta.url)).filter(name => /\.tsx?$/.test(name))) {
    const tree = ts.createSourceFile(file, fs.readFileSync(new URL('../src/' + file, import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    function visit(node) {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 't' && node.arguments[0] && ts.isStringLiteral(node.arguments[0])) {
        assert.ok(Object.hasOwn(english, node.arguments[0].text), `${file}: ${node.arguments[0].text}`);
      }
      ts.forEachChild(node, visit);
    }
    visit(tree);
  }
});

test('server diagnostics translate while Russian publication content stays intact', async () => {
  const title = 'Настройки';
  globalThis.fetch = async () => new Response(JSON.stringify({
    title, text: 'Готово', caption: 'Опубликовано', tags: ['Черновик'],
    message: 'AI-задача отменена', blocked_reason: 'Канал выключен', readiness: { reasons: ['Выберите хотя бы одно изображение'] },
    warnings: ['Источник недоступен'], stages: [{ key: 'ready', status: 'ready', title: 'Проверка черновика' }],
  }), { headers: { 'content-type': 'application/json' } });
  const result = await api('/test');
  assert.equal(result.title, title);
  assert.equal(result.text, 'Готово');
  assert.equal(result.caption, 'Опубликовано');
  assert.deepEqual(result.tags, ['Черновик']);
  assert.equal(result.message, 'AI task cancelled');
  assert.equal(result.blocked_reason, 'Channel disabled');
  assert.deepEqual(result.readiness.reasons, ['Select at least one image']);
  assert.deepEqual(result.warnings, ['Source unavailable']);
  assert.equal(result.stages[0].label, 'Reviewing draft');
  setLanguage('ru');
  assert.equal((await api('/test')).message, 'AI-задача отменена');
});
