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
 * Create HTTP server
 */
const server = http.createServer((req, res) => {
    const parsedUrl = url.parse(req.url, true);
    const path = parsedUrl.pathname;
    const method = req.method;
    
    console.log(`${method} ${path}`);
    
    // Set CORS headers
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    
    // Handle OPTIONS request
    if (method === 'OPTIONS') {
        res.writeHead(200);
        res.end();
        return;
    }
    
    // Route handling
    if (path === '/' && method === 'GET') {
        handleHome(req, res);
    } else if (path === '/api/health' && method === 'GET') {
        handleHealth(req, res);
    } else if (path === '/api/{{API_ENDPOINT}}') {
        handleApiEndpoint(req, res, method);
    } else {
        handleNotFound(req, res);
    }
});

/**
 * Handle home route
 */
function handleHome(req, res) {
    const response = {
        message: 'Welcome to {{APP_NAME}}',
        status: 'running',
        version: '1.0.0'
    };
    
    sendJsonResponse(res, 200, response);
}

/**
 * Handle health check
 */
function handleHealth(req, res) {
    const response = {
        status: 'healthy',
        service: '{{APP_NAME}}',
        timestamp: new Date().toISOString()
    };
    
    sendJsonResponse(res, 200, response);
}

/**
 * Handle API endpoint
 */
function handleApiEndpoint(req, res, method) {
    if (method === 'GET') {
        const response = {
            message: '{{API_MESSAGE}}',
            method: 'GET'
        };
        sendJsonResponse(res, 200, response);
    } else if (method === 'POST') {
        let body = '';
        
        req.on('data', chunk => {
            body += chunk.toString();
        });
        
        req.on('end', () => {
            let data;
            try {
                data = JSON.parse(body);
            } catch (e) {
                sendJsonResponse(res, 400, { error: 'Invalid JSON' });
                return;
            }
            
            const response = {
                message: 'Data received',
                data: data,
                method: 'POST'
            };
            sendJsonResponse(res, 201, response);
        });
    } else {
        sendJsonResponse(res, 405, { error: 'Method not allowed' });
    }
}

/**
 * Handle 404
 */
function handleNotFound(req, res) {
    sendJsonResponse(res, 404, {
        error: 'Not found',
        message: 'The requested resource was not found'
    });
}

/**
 * Send JSON response
 */
function sendJsonResponse(res, statusCode, data) {
    const json = JSON.stringify(data, null, 2);
    res.writeHead(statusCode, {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(json)
    });
    res.end(json);
}

/**
 * Start server
 */
server.listen(PORT, HOST, () => {
    console.log(`{{APP_NAME}} server running on http://${HOST}:${PORT}`);
    console.log(`Press Ctrl+C to stop`);
});

/**
 * Handle graceful shutdown
 */
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

