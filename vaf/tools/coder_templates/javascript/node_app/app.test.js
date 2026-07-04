/**
 * Tests for {{APP_NAME}} (run with: node --test).
 *
 * These test the pure `route()` function - no server, no network. They pass out of the
 * box; update them when you replace the '/api/{{API_ENDPOINT}}' logic in app.js.
 */
const test = require('node:test');
const assert = require('node:assert');
const { route } = require('./app');

test('health route returns healthy', () => {
    const r = route('GET', '/api/health', null);
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.body.status, 'healthy');
});

test('unknown route returns 404', () => {
    assert.strictEqual(route('GET', '/nope', null).status, 404);
});

test('api endpoint echoes POST body', () => {
    // REPLACE with assertions for your real logic.
    const r = route('POST', '/api/{{API_ENDPOINT}}', { a: 1 });
    assert.strictEqual(r.status, 201);
    assert.deepStrictEqual(r.body.data, { a: 1 });
});
