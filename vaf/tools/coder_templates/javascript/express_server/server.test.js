/**
 * Tests for {{APP_NAME}} (run with: node --test, after `npm install`).
 *
 * These test the pure `apiEndpoint()` logic - no server, no network. If dependencies are
 * not installed yet, they skip cleanly. Update them when you replace the endpoint logic.
 */
const test = require('node:test');
const assert = require('node:assert');

let apiEndpoint;
try {
    ({ apiEndpoint } = require('./server'));
} catch (e) {
    test('install dependencies first (npm install)', { skip: true }, () => {});
}

if (apiEndpoint) {
    test('GET returns 200 with a message', () => {
        const r = apiEndpoint('GET', null, {});
        assert.strictEqual(r.status, 200);
        assert.ok(r.body.message);
    });

    test('POST echoes the body with 201', () => {
        // REPLACE with assertions for your real logic.
        const r = apiEndpoint('POST', { a: 1 }, {});
        assert.strictEqual(r.status, 201);
        assert.deepStrictEqual(r.body.data, { a: 1 });
    });
}
