"""原始来源响应及一次请求读取；不自动解释业务完整性。"""
import base64
import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Response:
    source_id: str
    url: str
    status: int
    headers: tuple
    body: bytes

    def header(self,name):
        return ', '.join(value for key,value in self.headers if key.lower()==name.lower()) or None

    @property
    def sha256(self):return hashlib.sha256(self.body).hexdigest()

def from_packet(packet):
    if not isinstance(packet.get('source_id'),str) or not packet['source_id']:raise ValueError('来源身份无效')
    status=packet.get('status')
    if type(status) is not int or not 100<=status<=599:raise ValueError('响应状态无效')
    headers=packet.get('headers')
    if not isinstance(headers,list):raise ValueError('响应头无效')
    pairs=[]
    for pair in headers:
        if not isinstance(pair,list) or len(pair)!=2 or not all(isinstance(v,str) for v in pair):raise ValueError('响应头结构无效')
        if not pair[0] or any(x in pair[0]+pair[1] for x in ('\r','\n')):raise ValueError('响应头注入')
        pairs.append(tuple(pair))
    try:body=base64.b64decode(packet['body_base64'],validate=True)
    except Exception as exc:raise ValueError('响应字节编码无效') from exc
    return Response(packet['source_id'],packet['url'],status,tuple(pairs),body)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

def fetch(url,source_id,*,method='GET',headers=None,body=None,max_bytes=1048576):
    if type(max_bytes) is not int or max_bytes<0:raise ValueError('读取预算无效')
    request=urllib.request.Request(url,data=body,headers=headers or {},method=method)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:stream=opener.open(request,timeout=5)
    except urllib.error.HTTPError as exc:stream=exc
    with stream:
        data=stream.read(max_bytes+1)
        if len(data)>max_bytes:raise ValueError('响应超过读取预算')
        return Response(source_id,stream.geturl(),stream.code,tuple(stream.headers.raw_items()),data)


class _BudgetReader:
    """只读一次的实体流：强制执行字节预算并同步累计sha256，避免整份实体常驻。"""

    def __init__(self,raw,max_bytes):
        self._raw=raw
        self._budget=max_bytes
        self.digest=hashlib.sha256()
        self._read=0

    def read(self,size=-1):
        if size is None or size<0:
            chunk=self._raw.read(max(self._budget-self._read+1,0))
        else:
            chunk=self._raw.read(min(size,self._budget-self._read+1))
        self._read+=len(chunk)
        if self._read>self._budget:
            raise ValueError('响应超过读取预算')
        if chunk:
            self.digest.update(chunk)
        return chunk

    def readline(self):
        line=self._raw.readline(self._budget-self._read+1)
        self._read+=len(line)
        if self._read>self._budget:
            raise ValueError('响应超过读取预算')
        if line:
            self.digest.update(line)
        return line

    def close(self):
        self._raw.close()

    @property
    def bytes_read(self):return self._read


class StreamedResponse:
    """流式来源响应：实体按需读取，关闭后给出已读字节的sha256。"""

    def __init__(self,source_id,url,status,headers,reader):
        self.source_id=source_id
        self.url=url
        self.status=status
        self.headers=tuple(headers)
        self._reader=reader
        self._closed=False

    def header(self,name):
        return ', '.join(value for key,value in self.headers if key.lower()==name.lower()) or None

    @property
    def stream(self):return self._reader

    @property
    def sha256(self):
        if not self._closed:raise RuntimeError('响应未读完前没有完整摘要')
        return self._reader.digest.hexdigest()

    @property
    def bytes_read(self):return self._reader.bytes_read

    def close(self):
        if not self._closed:
            self._closed=True
            self._reader.close()

    def __enter__(self):return self

    def __exit__(self,*exc):
        self.close()
        return False


class _MemoryReader:
    """把已缓存字节伪装成实体流，供对Response或fixture复用流式解析路径。"""

    def __init__(self,data):
        import io
        self._buffer=io.BytesIO(data)
        self.digest=hashlib.sha256()

    def read(self,size=-1):
        chunk=self._buffer.read(size)
        if chunk:self.digest.update(chunk)
        return chunk

    def readline(self):
        line=self._buffer.readline()
        if line:self.digest.update(line)
        return line

    def close(self):self._buffer.close()

    @property
    def bytes_read(self):return self._buffer.tell()


def stream_from_bytes(source_id,url,status,headers,body,*,max_bytes=None):
    """对既有字节构造流式响应；超过预算同样失败，保证两条读取路径行为一致。"""
    if max_bytes is None:max_bytes=len(body)
    if type(max_bytes) is not int or max_bytes<0:raise ValueError('读取预算无效')
    if len(body)>max_bytes:raise ValueError('响应超过读取预算')
    return StreamedResponse(source_id,url,status,headers,_MemoryReader(body))


def open_stream(url,source_id,*,method='GET',headers=None,body=None,max_bytes=1048576):
    """打开一次响应并保留实体流；不自动跳转，不消费整个实体。调用方负责close。"""
    if type(max_bytes) is not int or max_bytes<0:raise ValueError('读取预算无效')
    request=urllib.request.Request(url,data=body,headers=headers or {},method=method)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:raw=opener.open(request,timeout=5)
    except urllib.error.HTTPError as exc:raw=exc
    return StreamedResponse(source_id,raw.geturl(),raw.code,tuple(raw.headers.raw_items()),_BudgetReader(raw,max_bytes))
