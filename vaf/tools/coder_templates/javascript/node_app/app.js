#!/usr/bin/env node
/**
 * {{APP_NAME}} - Node.js Application
 *
 * {{APP_DESCRIPTION}}
 */

const http = require('http');
const url = require('url');

const PORT = {{PORT}};
const HOST = '0.0.0.0';

/**
 * Pure routing logic: given a method, path and (already parsed) request body, return
 * { status, body }. Kept free of `req`/`res` so it is easy to unit test (see app.test.js).
 *
 * REPLACE the '/api/{{API_ENDPOINT}}' branch with the real logic for your task. The other
 * branches (home, health, 404) are a working baseline you can keep.
 */
function route(method, path, body) {
    if (path === '/' && method === 'GET') {
        return { status: 200, body: { message: 'Welcome to {{APP_NAME}}', status: 'running', version: '1.0.0' } };
    }
    if (path === '/api/health' && method === 'GET') {
        return { status: 200, body: { status: 'healthy', service: '{{APP_NAME}}' } };
    }
    if (path === '/api/{{API_ENDPOINT}}') {
        if (method === 'GET') {
            return { status: 200, body: { message: '{{API_MESSAGE}}', method: 'GET' } };
        }
        if (method === 'POST') {
            return { status: 201, body: { message: 'Data received', data: body, method: 'POST' } };
        }
        return { status: 405, body: { error: 'Method not allowed' } };
    }
    return { status: 404, body: { error: 'Not found', message: 'The requested resource was not found' } };
}

/**
 * HTTP server: reads the body (for writes), delegates to route(), sends JSON.
 */
const server = http.createServer((req, res) => {
    const parsedUrl = url.parse(req.url, true);
    const path = parsedUrl.pathname;
    const method = req.method;
    console.log(`${method} ${path}`);

    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    if (method === 'OPTIONS') {
        res.writeHead(200);
        res.end();
        return;
    }

    const send = ({ status, body }) => {
        const json = JSON.stringify(body, null, 2);
        res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(json) });
        res.end(json);
    };

    if (method === 'POST' || method === 'PUT') {
        let raw = '';
        req.on('data', (chunk) => { raw += chunk.toString(); });
        req.on('end', () => {
            let body = null;
            if (raw) {
                try { body = JSON.parse(raw); } catch (e) { return send({ status: 400, body: { error: 'Invalid JSON' } }); }
            }
            send(route(method, path, body));
        });
    } else {
        send(route(method, path, null));
    }
});

// Start the server only when run directly, so tests can import route()/server safely.
if (require.main === module) {
    server.listen(PORT, HOST, () => {
        console.log(`{{APP_NAME}} server running on http://${HOST}:${PORT}`);
        console.log('Press Ctrl+C to stop');
    });
    const shutdown = (sig) => {
        console.log(`${sig} received, shutting down gracefully...`);
        server.close(() => { console.log('Server closed'); process.exit(0); });
    };
    process.on('SIGTERM', () => shutdown('SIGTERM'));
    process.on('SIGINT', () => shutdown('SIGINT'));
}

module.exports = { route, server };
