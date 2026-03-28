#!/usr/bin/env python3
"""
Simple Webhook Server für Git Pull
Lauscht auf Port 8888 und führt git pull bei POST Request aus.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess
import json
import os
from datetime import datetime

# Security Token (ändern!)
SECRET_TOKEN = "apex-git-pull-secret-2026"
REPO_PATH = "/data/.openclaw/workspace/projects/apex-trading"

class WebhookHandler(BaseHTTPRequestHandler):
    
    def do_POST(self):
        # Check Token
        auth_header = self.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer ') or auth_header[7:] != SECRET_TOKEN:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return
        
        # Execute git pull
        try:
            os.chdir(REPO_PATH)
            result = subprocess.run(
                ['git', 'pull', 'origin', 'main'],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            response = {
                "status": "success",
                "timestamp": datetime.now().isoformat(),
                "output": result.stdout,
                "error": result.stderr if result.returncode != 0 else None
            }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response, indent=2).encode())
            
            print(f"[{datetime.now()}] Git pull executed: {result.stdout}")
            
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
    
    def log_message(self, format, *args):
        print(f"[{datetime.now()}] {format % args}")

if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', 8888), WebhookHandler)
    print(f"🚀 Webhook server running on port 8888")
    print(f"📁 Repo: {REPO_PATH}")
    print(f"🔐 Token: {SECRET_TOKEN}")
    server.serve_forever()
