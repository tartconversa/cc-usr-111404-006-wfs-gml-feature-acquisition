"""WFS2.0协议客户端：能力确认、类型描述、GetFeature与分页。

不把HTTP 200当成成功：OWS ExceptionReport可能藏在200响应里，按来源异常处理。
分页只沿用来源返回的next链接或在来源声明支持稳定排序时推进startIndex，
过滤条件与排序参数在整轮采集中保持不变。
"""
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import source as source_mod
from .gmlstream import (
    safe_xml_parser, local_name, namespace_of,
    parse_exception_report, sniff_root,
)

WFS_NS = 'http://www.opengis.net/wfs/2.0'
OWS11_NS = 'http://www.opengis.net/ows/1.1'
XLINK_NS = 'http://www.w3.org/1999/xlink'
XLINK_HREF = '{%s}href' % XLINK_NS


class WfsException(Exception):
    """来源以ExceptionReport或非WFS结构表达的协议异常。"""

    def __init__(self, message, *, status=None, report=None, url=None):
        super().__init__(message)
        self.status = status
        self.report = report
        self.url = url


@dataclass
class OperationConstraint:
    name: str
    allowed_values: tuple = ()
    default: str | None = None
    raw: str | None = None


@dataclass
class OperationMeta:
    name: str
    get_url: str | None = None
    post_url: str | None = None
    constraints: dict = field(default_factory=dict)


@dataclass
class Capabilities:
    version: str | None
    default_count: int | None
    operations: dict                       # 操作名 -> OperationMeta
    constraints: dict                      # 全局约束名 -> OperationConstraint
    feature_type_names: tuple
    output_formats: tuple
    raw_url: str

    def constraint_true(self, name):
        constraint = self.constraints.get(name)
        return bool(constraint and constraint.default == 'TRUE')

    def supports_paging(self):
        return self.constraint_true('ImplementsResultSetPaging')

    def supports_stable_sort(self):
        # WFS2.0没有标准的“稳定快照”声明；Resolves…之外，排序稳定性只能由
        # 部署方扩展约束或调用方配置确认，默认视为不稳定。
        constraint = self.constraints.get('ImplementsStableSort')
        return bool(constraint and constraint.default == 'TRUE')


@dataclass
class FeatureTypeDescription:
    type_name: str
    is_abstract: bool
    properties: tuple                      # (命名空间限定名, 类型本地名, 是否几何)
    geometry_properties: tuple
    raw_xml: str


@dataclass
class PageRequest:
    """一页GetFeature的可复现请求描述；后续页必须保持同一查询口径。"""
    url: str
    method: str
    type_names: str
    params: dict
    body: bytes | None
    sort_by: str | None
    filter_xml: str | None
    start_index: int
    count: int | None


# ---------- 响应体检 ----------

def ensure_feature_collection(streamed, body_bytes):
    """把来源响应统一分类：ExceptionReport / 非WFS / 可解析集合。"""
    report = parse_exception_report(body_bytes)
    if report is not None:
        raise WfsException(
            '来源返回ExceptionReport而非要素集合（HTTP %s）' % streamed.status,
            status=streamed.status, report=report, url=streamed.url)
    try:
        root = ET.fromstring(body_bytes, parser=safe_xml_parser())
    except ET.ParseError as exc:
        raise WfsException('响应既不是ExceptionReport也不是可解析XML: %s' % exc,
                           status=streamed.status, url=streamed.url) from exc
    if local_name(root.tag) != 'FeatureCollection' or namespace_of(root.tag) != WFS_NS:
        raise WfsException('响应根元素不是wfs:FeatureCollection: %r' % root.tag,
                           status=streamed.status, url=streamed.url)
    return root


# ---------- Capabilities ----------

def _constraint(elem):
    name = elem.get('name')
    default_elem = elem.find('{%s}DefaultValue' % OWS11_NS)
    default = default_elem.text.strip() if default_elem is not None and \
        default_elem.text else elem.get('default')
    values = tuple(v.text.strip() for v in
                   elem.findall('.//{%s}Value' % OWS11_NS)
                   if v.text and v.text.strip())
    no_values = elem.findall('.//{%s}NoValues' % OWS11_NS)
    raw = None if no_values else (elem.text or '').strip() or None
    # 无AllowedValues的简单约束（如CountDefault）可能直接把值放在DefaultValue
    if raw is None and default is not None:
        raw = default
    return name, OperationConstraint(name, values, default, raw)


def parse_capabilities(body_bytes, url):
    try:
        root = ET.fromstring(body_bytes, parser=safe_xml_parser())
    except ET.ParseError as exc:
        raise WfsException('Capabilities无法解析: %s' % exc, url=url) from exc
    if local_name(root.tag) != 'WFS_Capabilities':
        report = parse_exception_report(body_bytes)
        raise WfsException('不是WFS Capabilities文档', report=report, url=url)

    operations = {}
    for op in root.findall('.//{%s}Operation' % WFS_NS):
        get = op.find('{%s}DCP/{%s}HTTP/{%s}Get' % (OWS11_NS, OWS11_NS, OWS11_NS))
        post = op.find('{%s}DCP/{%s}HTTP/{%s}Post' % (OWS11_NS, OWS11_NS, OWS11_NS))
        constraints = dict(_constraint(c) for c in
                           op.findall('{%s}Constraint' % OWS11_NS))
        operations[op.get('name')] = OperationMeta(
            op.get('name'),
            get.get(XLINK_HREF) if get is not None else None,
            post.get(XLINK_HREF) if post is not None else None,
            constraints)

    constraints = {}
    # WFS2.0规定OperationsMetadata属于ows命名空间；个别部署误用wfs命名空间，两者兼容
    operations_metadata = root.find('{%s}OperationsMetadata' % OWS11_NS)
    if operations_metadata is None:
        operations_metadata = root.find('{%s}OperationsMetadata' % WFS_NS)
    if operations_metadata is not None:
        constraints = dict(_constraint(c) for c in
                           operations_metadata.findall('{%s}Constraint' % OWS11_NS))

    default_count = None
    count_constraint = constraints.get('CountDefault')
    if count_constraint:
        candidate = count_constraint.default or count_constraint.raw
        if candidate and candidate.isdigit():
            default_count = int(candidate)

    feature_names = tuple(
        (name.text or '').strip()
        for ft in root.findall('.//{%s}FeatureType' % WFS_NS)
        for name in [ft.find('{%s}Name' % WFS_NS)]
        if name is not None and (name.text or '').strip())
    output_formats = ()
    fmt = constraints.get('GetFeatureOutputFormats')
    if fmt and fmt.allowed_values:
        output_formats = fmt.allowed_values

    return Capabilities(
        version=root.get('version'),
        default_count=default_count,
        operations=operations,
        constraints=constraints,
        feature_type_names=feature_names,
        output_formats=output_formats,
        raw_url=url)


# ---------- DescribeFeatureType ----------

def parse_feature_type(body_bytes, expected_name):
    try:
        root = ET.fromstring(body_bytes, parser=safe_xml_parser())
    except ET.ParseError as exc:
        raise WfsException('DescribeFeatureType无法解析: %s' % exc) from exc
    if local_name(root.tag) == 'ExceptionReport':
        report = parse_exception_report(body_bytes)
        raise WfsException('DescribeFeatureType返回ExceptionReport', report=report)
    if local_name(root.tag) != 'schema':
        raise WfsException('DescribeFeatureType响应不是XML Schema: %r' % root.tag)
    xsd_ns = namespace_of(root.tag) or 'http://www.w3.org/2001/XMLSchema'
    candidates = [elem for elem in root.findall('{%s}element' % xsd_ns)
                  if elem.get('name') and _matches_type(elem.get('name'), expected_name)]
    if not candidates:
        raise WfsException('Schema中找不到要素类型 %r' % expected_name)
    elem = candidates[0]
    properties, geometry_properties = [], []
    complex_type = elem.find('{%s}complexType' % xsd_ns)
    if complex_type is None:
        type_ref = elem.get('type')
        if type_ref:
            prefix, _, local = type_ref.partition(':')
            ns = elem.get('xmlns:' + prefix) if prefix else None
            for candidate in root.findall('{%s}complexType' % xsd_ns):
                if candidate.get('name') == local:
                    complex_type = candidate
                    break
    if complex_type is not None:
        for prop in complex_type.findall('.//{%s}element' % xsd_ns):
            prop_name = prop.get('name')
            if not prop_name:
                continue
            type_text = prop.get('type') or ''
            type_local = type_text.split(':')[-1]
            is_geom = ('gml' in type_text.lower()
                       or type_local in {'AbstractGeometryType', 'PointType',
                                         'CurveType', 'SurfaceType',
                                         'MultiSurfaceType', 'MultiPointType',
                                         'MultiCurveType'})
            properties.append((prop_name, type_local, is_geom))
            if is_geom:
                geometry_properties.append(prop_name)
    return FeatureTypeDescription(
        type_name=elem.get('name'),
        is_abstract=(elem.get('abstract') == 'true'),
        properties=tuple(properties),
        geometry_properties=tuple(geometry_properties),
        raw_xml=ET.tostring(root, encoding='unicode')[:8000])


def _matches_type(schema_name, expected):
    if schema_name == expected:
        return True
    return schema_name.split(':')[-1] == expected.split(':')[-1]


# ---------- 协议封装 ----------

class WfsClient:
    def __init__(self, endpoint, source_id, *, max_bytes=8 * 1024 * 1024,
                 extra_params=None, headers=None):
        self.endpoint = endpoint
        self.source_id = source_id
        self.max_bytes = max_bytes
        self.extra_params = dict(extra_params or {})
        self.headers = dict(headers or {})
        self.request_log = []

    def _read_full(self, url, *, method='GET', body=None, content_type=None):
        headers = dict(self.headers)
        if content_type:
            headers['Content-Type'] = content_type
        streamed = source_mod.open_stream(
            url, self.source_id, method=method, headers=headers, body=body,
            max_bytes=self.max_bytes)
        try:
            data = streamed.stream.read()
            sha = None
        finally:
            streamed.close()
        sha = streamed.sha256
        self.request_log.append({
            'method': method, 'url': streamed.url, 'status': streamed.status,
            'request_body': body, 'response_sha256': sha,
            'bytes_read': streamed.bytes_read,
        })
        return streamed, data

    def get_capabilities(self):
        params = {'service': 'WFS', 'version': '2.0.0', 'request': 'GetCapabilities'}
        params.update(self.extra_params)
        url = self.endpoint + ('&' if '?' in self.endpoint else '?') + \
            urllib.parse.urlencode(params)
        streamed, data = self._read_full(url)
        report = parse_exception_report(data)
        if report is not None:
            raise WfsException('GetCapabilities返回ExceptionReport',
                               status=streamed.status, report=report, url=url)
        return parse_capabilities(data, url)

    def describe_feature_type(self, type_name, *, capabilities=None):
        params = {'service': 'WFS', 'version': '2.0.0',
                  'request': 'DescribeFeatureType', 'typeNames': type_name}
        params.update(self.extra_params)
        base = self.endpoint
        if capabilities and 'DescribeFeatureType' in capabilities.operations:
            meta = capabilities.operations['DescribeFeatureType']
            base = meta.get_url or meta.post_url or base
        url = base + ('&' if '?' in base else '?') + urllib.parse.urlencode(params)
        streamed, data = self._read_full(url)
        return parse_feature_type(data, type_name)

    def build_page_request(self, type_name, *, count=None, filter_xml=None,
                           sort_by=None, start_index=0, output_format=None,
                           method='GET'):
        params = {'service': 'WFS', 'version': '2.0.0',
                  'request': 'GetFeature', 'typeNames': type_name,
                  'startIndex': str(start_index)}
        if count is not None:
            params['count'] = str(count)
        if sort_by:
            params['sortBy'] = sort_by
        if output_format:
            params['outputFormat'] = output_format
        if filter_xml and method == 'GET':
            params['filter'] = filter_xml
        params.update(self.extra_params)
        body = None
        url = self.endpoint + ('&' if '?' in self.endpoint else '?') + \
            urllib.parse.urlencode(params)
        if method == 'POST':
            for key in ('filter',):
                params.pop(key, None)
            body = _get_feature_post_xml(type_name, filter_xml, sort_by,
                                         count, start_index, output_format)
            url = self.endpoint
        return PageRequest(url=url, method=method, type_names=type_name,
                           params=params, body=body, sort_by=sort_by,
                           filter_xml=filter_xml, start_index=start_index,
                           count=count)

    def open_page(self, page_request):
        """打开一页GetFeature并完成来源异常体检。

        正常FeatureCollection以流式返回（调用方负责close）；只有ExceptionReport
        或非WFS响应才在错误路径读全实体并抛WfsException，绝不把它记成空要素集。
        """
        content_type = 'application/xml' if page_request.method == 'POST' else None
        streamed = source_mod.open_stream(
            page_request.url, self.source_id, method=page_request.method,
            headers={**self.headers,
                     **({'Content-Type': content_type} if content_type else {})},
            body=page_request.body, max_bytes=self.max_bytes)
        try:
            root_name, wrapped = sniff_root(streamed.stream)
        except Exception:
            streamed.close()
            raise
        streamed._reader = wrapped
        if root_name == 'ExceptionReport':
            data = wrapped.read()
            report = parse_exception_report(data)
            streamed.close()
            self.request_log.append({
                'method': page_request.method, 'url': streamed.url,
                'status': streamed.status, 'request_body': page_request.body,
                'response_sha256': streamed.sha256,
                'bytes_read': streamed.bytes_read, 'exception_report': True})
            raise WfsException(
                'HTTP %s响应体内是ExceptionReport，不是空要素集' % streamed.status,
                status=streamed.status, report=report, url=streamed.url)
        if root_name not in ('FeatureCollection', None):
            data = wrapped.read()
            streamed.close()
            raise WfsException('响应根元素%r不是wfs:FeatureCollection' % root_name,
                               status=streamed.status, url=page_request.url)
        self.request_log.append({
            'method': page_request.method, 'url': streamed.url,
            'status': streamed.status, 'request_body': page_request.body,
            'response_sha256': None, 'bytes_read': None})
        streamed.request_log_index = len(self.request_log) - 1
        return streamed

    def mark_page_completed(self, streamed):
        """页面读完关闭后，把真实sha256与字节数回填到请求日志。"""
        index = getattr(streamed, 'request_log_index', None)
        if index is None:
            return
        self.request_log[index].update(
            response_sha256=streamed.sha256, bytes_read=streamed.bytes_read)

    def follow_next(self, next_url, template):
        """来源给出next链接时沿用它，但校验过滤与排序口径没有被来源改写。"""
        parsed = urllib.parse.urlsplit(next_url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if template.filter_xml:
            if not query.get('filter'):
                raise WfsException('next链接丢失过滤条件，停止分页以避免口径漂移',
                                   url=next_url)
            if query['filter'][0] != template.filter_xml:
                raise WfsException('next链接改变了过滤条件，停止分页以避免口径漂移',
                                   url=next_url)
        if template.sort_by and query.get('sortBy', [None])[0] != template.sort_by:
            raise WfsException('next链接改变了排序参数，停止分页以避免口径漂移',
                               url=next_url)
        if query.get('typeNames', [template.type_names])[0] != template.type_names:
            raise WfsException('next链接改变了typeNames，停止分页', url=next_url)
        return PageRequest(url=next_url, method=template.method,
                           type_names=template.type_names, params=query,
                           body=template.body, sort_by=template.sort_by,
                           filter_xml=template.filter_xml,
                           start_index=_safe_int(query.get('startIndex', ['0'])[0]),
                           count=template.count)


def _safe_int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _get_feature_post_xml(type_name, filter_xml, sort_by, count,
                          start_index, output_format):
    prefix, _, local = type_name.partition(':')
    ns_map = ('xmlns:wfs="%s" xmlns:fes="http://www.opengis.net/fes/2.0"'
              % WFS_NS)
    attrs = ['service="WFS" version="2.0.0"', ns_map]
    if count is not None:
        attrs.append('count="%d"' % count)
    attrs.append('startIndex="%d"' % start_index)
    if output_format:
        attrs.append('outputFormat="%s"' % output_format)
    query_attrs = 'typeNames="%s"' % type_name
    if sort_by:
        sort_terms = ''.join(
            '<fes:SortProperty><fes:ValueReference>%s</fes:ValueReference></fes:SortProperty>'
            % term.split()[0] for term in sort_by.split(','))
        sort_block = '<fes:SortBy>%s</fes:SortBy>' % sort_terms
    else:
        sort_block = ''
    filter_block = filter_xml or ''
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<wfs:GetFeature %s><wfs:Query %s>%s%s</wfs:Query></wfs:GetFeature>'
           % (' '.join(attrs), query_attrs, filter_block, sort_block))
    return xml.encode('utf-8')


def extract_next_link(root):
    """从已解析的FeatureCollection根元素取wfs:next href。"""
    link = root.find('{%s}next' % WFS_NS)
    if link is None:
        return None
    return link.get(XLINK_HREF)
