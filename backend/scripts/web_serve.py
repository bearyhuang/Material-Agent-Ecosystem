#!/usr/bin/env python3
"""静态页面 + /api 同源反向代理。

外网/路由器只需放通一个端口即可同时访问页面与 API：
    http://<host>:8099/          -> 返回 frontend/ 静态文件
    http://<host>:8099/api/...   -> 转发到 127.0.0.1:8000（容器映射的 FastAPI）

用法：python3 web_serve.py [port]        # 默认 8099
环境变量：FS_API_BASE（默认 http://127.0.0.1:8000）
"""
import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.request

WEB_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend")
)
API_BASE = os.environ.get("FS_API_BASE", "http://127.0.0.1:8000").rstrip("/")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    # ---- /api 反向代理 ----
    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(API_BASE + self.path, data=body, method=self.command)
        for key, val in self.headers.items():
            if key.lower() not in ("host", "content-length", "connection"):
                req.add_header(key, val)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for key, val in resp.headers.items():
                    if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(key, val)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:  # 上游不可达
            data = json.dumps({"error": "upstream unreachable", "detail": str(exc)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api"):
            return self._proxy()
        return super().do_GET()

    def do_HEAD(self):
        if self.path.startswith("/api"):
            return self._proxy()
        return super().do_HEAD()

    def do_POST(self):
        if self.path.startswith("/api"):
            return self._proxy()
        self.send_error(405)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        print("web/ + /api proxy on 0.0.0.0:%d  (upstream %s)" % (PORT, API_BASE), flush=True)
        httpd.serve_forever()
