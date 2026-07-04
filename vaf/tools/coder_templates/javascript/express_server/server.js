#!/usr/bin/env node
/**
 * {{APP_NAME}} - Express.js Server
 *
 * {{APP_DESCRIPTION}}
 */

const express = require('express');
const cors = require('cors');

const app = express();
const PORT = {{PORT}};
const HOST = '0.0.0.0';

// Middleware
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use((req, res, next) => {
    console.log(`${req.method} ${req.path}`);
    next();
});

/**
 * Pure API logic: given method/body/query, return { status, body }. Kept req/res-free so
 * it is easy to unit test (see server.test.js).
 *
 * REPLACE this with the real logic for your task; add more such functions and routes the
 * same way.
 */
function apiEndpoint(method, body, query) {
    if (method === 'GET') {
        return { status: 200, body: { message: '{{API_MESSAGE}}', method: 'GET', query: query || {} } };
    }
    if (method === 'POST') {
        return { status: 201, body: { message: 'Data received', data: body, method: 'POST' } };
    }
    return { status: 405, body: { error: 'Method not allowed' } };
}

// Routes
app.get('/', (req, res) => {
    res.json({ message: 'Welcome to {{APP_NAME}}', status: 'running', version: '1.0.0' });
});

app.get('/api/health', (req, res) => {
    res.json({ status: 'healthy', service: '{{APP_NAME}}' });
});

app.get('/api/{{API_ENDPOINT}}', (req, res) => {
    const { status, body } = apiEndpoint('GET', null, req.query);
    res.status(status).json(body);
});

app.post('/api/{{API_ENDPOINT}}', (req, res) => {
    const { status, body } = apiEndpoint('POST', req.body, req.query);
    res.status(status).json(body);
});

// Error handling middleware
app.use((err, req, res, next) => {
    console.error('Error:', err);
    res.status(err.status || 500).json({ error: 'Internal server error', message: err.message });
});

// 404 handler
app.use((req, res) => {
    res.status(404).json({ error: 'Not found', message: 'The requested resource was not found', path: req.path });
});

// Start the server only when run directly, so tests can import the app safely.
if (require.main === module) {
    const server = app.listen(PORT, HOST, () => {
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

module.exports = { app, apiEndpoint };
