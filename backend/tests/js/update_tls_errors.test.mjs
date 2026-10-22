import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../../src/web/app/core/api.js', import.meta.url), 'utf8');
const {friendlyError, GENERIC_ERROR} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

// Each new updater code must show its own Vietnamese cause, not the generic fallback
// and not the old catch-all "Không kết nối được GitHub" text.
const CASES = {
  catalog_tls_error: /chứng chỉ|bảo mật|TLS/i,
  catalog_dns_error: /DNS|tên miền|phân giải/i,
  catalog_timeout: /chậm|quá giờ|hết thời gian/i,
  catalog_proxy_error: /proxy|VPN/i,
  package_tls_error: /chứng chỉ|bảo mật|TLS/i,
  package_dns_error: /DNS|tên miền|phân giải/i,
  package_proxy_error: /proxy|VPN/i,
};

for (const [code, cause] of Object.entries(CASES)) {
  test(`updater code ${code} has a specific Vietnamese message`, () => {
    const text = friendlyError({code});
    assert.ok(!text.startsWith(GENERIC_ERROR), `${code} fell back to generic: ${text}`);
    assert.match(text, /[ạảãáàâầấậẩẫăằắặẳẵđêềếệểễôồốộổỗơờớợởỡưừứựửữ]/i, `${code} not Vietnamese: ${text}`);
    assert.match(text, cause, `${code} does not name the cause: ${text}`);
    assert.notEqual(text, 'Không kết nối được GitHub. Kiểm tra mạng rồi thử lại.');
  });
}

test('catalog_backoff explains the server is refusing and to wait, without blaming the network', () => {
  const text = friendlyError({code: 'catalog_backoff'});
  assert.ok(!text.startsWith(GENERIC_ERROR));
  assert.match(text, /GitHub|máy chủ/i);
  assert.doesNotMatch(text, /kiểm tra mạng/i);
  assert.match(text, /chờ|đợi/i);
});

test('generic catalog_network_error still has a Vietnamese message', () => {
  const text = friendlyError({code: 'catalog_network_error'});
  assert.ok(!text.startsWith(GENERIC_ERROR));
  assert.match(text, /GitHub/);
});
