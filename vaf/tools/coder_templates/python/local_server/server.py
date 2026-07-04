#!/usr/bin/env python3
"""
{{APP_NAME}} - Local Development Server

{{APP_DESCRIPTION}}
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import os

app = Flask(__name__)
CORS(app)  # Enable CORS for development

# Configuration
PORT = {{PORT}}
DEBUG = True


@app.route('/')
def home():
    """Home endpoint."""
    return jsonify({
        'message': 'Welcome to {{APP_NAME}}',
        'status': 'running',
        'version': '1.0.0'
    })


@app.route('/api/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'service': '{{APP_NAME}}'
    })


@app.route('/api/{{API_ENDPOINT}}', methods=['GET', 'POST'])
def api_endpoint():
    """
    Example API endpoint.  <-- REPLACE with the real endpoint(s) for your task.

    This working example (GET returns a message, POST echoes the body) keeps the
    scaffold runnable and test_server.py green out of the box. Add your own routes
    the same way and update the tests.

    GET: Returns data
    POST: Accepts data
    """
    if request.method == 'GET':
        return jsonify({
            'message': '{{API_MESSAGE}}',
            'method': 'GET'
        })
    
    elif request.method == 'POST':
        data = request.get_json()
        return jsonify({
            'message': 'Data received',
            'data': data,
            'method': 'POST'
        }), 201


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return jsonify({
        'error': 'Not found',
        'message': 'The requested resource was not found'
    }), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    return jsonify({
        'error': 'Internal server error',
        'message': 'An unexpected error occurred'
    }), 500


if __name__ == '__main__':
    print(f"Starting {{APP_NAME}} server on port {PORT}...")
    print(f"Debug mode: {DEBUG}")
    print(f"Access the API at: http://localhost:{PORT}")
    
    app.run(
        host='0.0.0.0',
        port=PORT,
        debug=DEBUG
    )

