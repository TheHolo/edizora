import { readFile } from 'node:fs/promises';
import openapiTS, { astToString, COMMENT_HEADER } from 'openapi-typescript';

const source = new URL('../../docs/openapi.json', import.meta.url);
const target = new URL('../src/generated/api.ts', import.meta.url);
const expected = COMMENT_HEADER + astToString(await openapiTS(source));
const actual = await readFile(target, 'utf8');
if (expected.replace(/\r\n/g, '\n') !== actual.replace(/\r\n/g, '\n')) {
  process.stderr.write('OpenAPI types are stale. Run npm run gen:api after exporting the backend schema.\n');
  process.exitCode = 1;
} else {
  process.stdout.write('OpenAPI types match docs/openapi.json.\n');
}
