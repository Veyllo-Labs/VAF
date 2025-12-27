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

// Request logging middleware
app.use((req, res, next) => {
    console.log(`${req.method} ${req.path}`);
    next();
});

// Routes
app.get('/', (req, res) => {
    res.json({
        message: 'Welcome to {{APP_NAME}}',
        status: 'running',
        version: '1.0.0'
    });
});

app.get('/api/health', (req, res) => {
    res.json({
        status: 'healthy',
        service: '{{APP_NAME}}',
        timestamp: new Date().toISOString()
    });
});

app.get('/api/{{API_ENDPOINT}}', (req, res) => {
    res.json({
        message: '{{API_MESSAGE}}',
        method: 'GET',
        query: req.query
    });
});

app.post('/api/{{API_ENDPOINT}}', (req, res) => {
    res.status(201).json({
        message: 'Data received',
        data: req.body,
        method: 'POST'
    });
});

// Error handling middleware
app.use((err, req, res, next) => {
    console.error('Error:', err);
    res.status(err.status || 500).json({
        error: 'Internal server error',
        message: err.message
    });
});

// 404 handler
app.use((req, res) => {
    res.status(404).json({
        error: 'Not found',
        message: 'The requested resource was not found',
        path: req.path
    });
});

// Start server
const server = app.listen(PORT, HOST, () => {
    console.log(`{{APP_NAME}} server running on http://${HOST}:${PORT}`);
    console.log(`Press Ctrl+C to stop`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
    console.log('SIGTERM received, shutting down gracefully...');
    server.close(() => {
        console.log('Server closed');
        process.exit(0);
    });
});

process.on('SIGINT', () => {
    console.log('\nSIGINT received, shutting down gracefully...');
    server.close(() => {
        console.log('Server closed');
        process.exit(0);
    });
});

