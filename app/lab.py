"""在回环地址提供可复现的来源响应，用于接口联调。"""
import base64
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

class SourceServer:
    def __init__(self,packet):
        self.packet=copy.deepcopy(packet);self.requests=[];self.lock=threading.Lock()

    def __enter__(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def handle_request(self):
                size=int(self.headers.get('Content-Length','0'));body=self.rfile.read(size)
                with owner.lock:owner.requests.append({'method':self.command,'path':self.path,'headers':list(self.headers.items()),'body_base64':base64.b64encode(body).decode()})
                if self.path!='/source':
                    self.send_response(404);self.send_header('Content-Length','0');self.end_headers();return
                packet=owner.packet;data=base64.b64decode(packet['body_base64'])
                self.send_response(packet['status'])
                for key,value in packet['headers']:self.send_header(key,value)
                if not any(k.lower()=='content-length' for k,v in packet['headers']):self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                if self.command!='HEAD':self.wfile.write(data)
            do_GET=do_POST=do_HEAD=do_PROPFIND=do_REPORT=handle_request
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.thread.start();self.url='http://127.0.0.1:'+str(self.server.server_port)+'/source'
        return self

    def __exit__(self,*args):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
        if self.thread.is_alive():raise RuntimeError('来源服务未停止')

def main():
    import sys
    from pathlib import Path
    packet=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    with SourceServer(packet) as server:
        print(server.url,flush=True)
        try:threading.Event().wait()
        except KeyboardInterrupt:pass


class ScriptedServer:
    """按路由函数返回不同响应的回环WFS来源，支持分页与异常报告联调。

    router(method, path, query, body) -> packet dict：
      {status, headers:[[k,v]...], body_base64} 或 None（404）。
    只绑定127.0.0.1，记录全部请求供断言。
    """

    def __init__(self, router):
        self.router = router
        self.requests = []
        self.lock = threading.Lock()

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                size = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(size)
                parts = urlsplit(self.path)
                query = {key: values[0] if len(values) == 1 else values
                         for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
                with owner.lock:
                    owner.requests.append({
                        'method': self.command, 'path': self.path,
                        'query': query,
                        'headers': list(self.headers.items()),
                        'body_base64': base64.b64encode(body).decode()})
                packet = owner.router(self.command, parts.path, query, body)
                if packet is None:
                    self.send_response(404)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                data = base64.b64decode(packet['body_base64'])
                self.send_response(packet.get('status', 200))
                for key, value in packet.get('headers', []):
                    self.send_header(key, value)
                if not any(k.lower() == 'content-length'
                           for k, _ in packet.get('headers', [])):
                    self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)

            do_GET = do_POST = do_HEAD = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('来源服务未停止')

if __name__=='__main__':main()
