"""WFS2.0/GML3.2响应的流式解析与要素级溯源。

设计约束：
- xml.etree.ElementTree.iterparse增量处理wfs:member，解析过的成员立即从树中
  清除，大响应不需要整份DOM常驻；
- 显式拒绝DOCTYPE与外部实体（含参数实体、外部DTD）；
- 几何按实际CRS标识解释轴序，无法解释的字段或几何生成issue并定位到要素与原
  响应片段，绝不删除要素后宣称成功；
- xlink:href不在本模块内抓取，只产出引用状态，由app.refs按授权策略解析。
"""
import xml.etree.ElementTree as ET
import xml.parsers.expat as _expat
import weakref
from dataclasses import dataclass, field

from . import crs

WFS_NS = 'http://www.opengis.net/wfs/2.0'
GML_NS = 'http://www.opengis.net/gml/3.2'
XLINK_NS = 'http://www.w3.org/1999/xlink'
WFS_MEMBER = '{%s}member' % WFS_NS
WFS_FEATURE_COLLECTION = '{%s}FeatureCollection' % WFS_NS
XLINK_HREF = '{%s}href' % XLINK_NS
GML_ID = '{%s}id' % GML_NS
GML_NIL = '{%s}nil' % GML_NS

# 可解释的GML几何元素本地名 -> 几何类别
_GEOMETRY_TAGS = {
    'Point', 'MultiPoint', 'LineString', 'MultiLineString',
    'Polygon', 'MultiSurface', 'MultiGeometry',
}
_RING_TAGS = {'LinearRing', 'Ring'}
_SURFACE_TAGS = {'Surface', 'CompositeSurface', 'Polygon'}


class GmlFormatError(ValueError):
    """响应不是可解释的WFS/GML结构。"""


class DtdForbidden(GmlFormatError):
    """响应携带DOCTYPE或外部实体声明，按契约拒绝。"""


class _SniffReader:
    """包装实体流：缓存读取过的前缀字节，iterparse从含前缀的拼接流继续消费。"""

    def __init__(self, raw):
        self._raw = raw
        self._prefix = b''
        self._prefix_pos = 0

    def feed_prefix(self, data):
        self._prefix += data

    @property
    def digest(self):
        return self._raw.digest

    @property
    def bytes_read(self):
        return self._raw.bytes_read

    def read(self, size=-1):
        if self._prefix_pos < len(self._prefix):
            if size is None or size < 0:
                chunk = self._prefix[self._prefix_pos:]
                self._prefix_pos = len(self._prefix)
                return chunk + self._raw.read()
            chunk = self._prefix[self._prefix_pos:self._prefix_pos + size]
            self._prefix_pos += len(chunk)
            if len(chunk) == size:
                return chunk
            return chunk + self._raw.read(size - len(chunk))
        return self._raw.read(size)

    def readline(self):
        if self._prefix_pos < len(self._prefix):
            line = self._prefix[self._prefix_pos:].split(b'\n', 1)[0]
            self._prefix_pos += len(line)
            return line
        return self._raw.readline()

    def close(self):
        self._raw.close()


def sniff_root(stream, *, probe=4096):
    """读取一小段前缀识别根元素本地名，返回 (本地名, 包装流)。

    包装流会先吐出前缀再委托原始实体，调用方应改用返回的流继续解析。
    """
    wrapped = stream if isinstance(stream, _SniffReader) else _SniffReader(stream)
    head = stream.read(probe) if isinstance(stream, _SniffReader) else wrapped._raw.read(probe)
    wrapped.feed_prefix(head)
    name = None
    text = head.decode('utf-8', errors='replace')
    marker = '<'
    for index, char in enumerate(text):
        if char == marker:
            rest = text[index + 1:]
            if rest.startswith('?') or rest.startswith('!'):
                continue
            tag = rest.split('>', 1)[0].split()[0] if rest else ''
            name = tag.split(':')[-1] if tag else None
            break
    return name, wrapped


def safe_xml_parser():
    """构造禁用DTD与外部实体的解析器（TreeBuilder驱动），供fromstring使用。"""
    return SafeTreeParser()


def _reject_dtd(*args, **kwargs):
    raise DtdForbidden('禁止DTD与外部实体声明')


class SafeTreeParser:
    """xml.parsers.expat直接驱动ElementTree.TreeBuilder。

    Python3.11的C版XMLParser不公开底层expat，无法挂DTD回调；这里自行持有
    expat，显式拒绝DOCTYPE、外部（含参数）实体，再把命名空间展开名翻译成
    ElementTree的{uri}local标签形态。
    """

    def __init__(self):
        self._expat = _expat.ParserCreate(namespace_separator='|')
        self._builder = ET.TreeBuilder()
        self._stack = []
        parser = self._expat
        parser.StartDoctypeDeclHandler = _reject_dtd
        parser.EndDoctypeDeclHandler = _reject_dtd
        parser.ExternalEntityRefHandler = _reject_dtd
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._data

    @staticmethod
    def _qname(name):
        if '|' in name:
            uri, local = name.split('|', 1)
            return ('{%s}%s' % (uri, local)) if uri else local
        return name

    def _start(self, name, attrs):
        tag = self._qname(name)
        fixed = {self._qname(key): value for key, value in attrs.items()}
        self._builder.start(tag, fixed)
        self._stack.append(tag)

    def _end(self, name):
        self._stack.pop()
        self._builder.end(self._qname(name))

    def _data(self, text):
        self._builder.data(text)

    def feed(self, data):
        try:
            self._expat.Parse(data, False)
        except _expat.ExpatError as exc:
            raise ET.ParseError(str(exc)) from exc

    def close(self):
        try:
            self._expat.Parse(b'', True)
        except _expat.ExpatError as exc:
            raise ET.ParseError(str(exc)) from exc
        return self._builder.close()


class _StreamingMemberParser:
    """增量解析并在每个wfs:member结束时回调，回调后立即摘除该成员DOM。"""

    def __init__(self, on_root, on_member):
        self._expat = _expat.ParserCreate(namespace_separator='|')
        self._builder = ET.TreeBuilder()
        self._stack = []
        self._root = None
        self._member_depth = None
        self._on_root = on_root
        self._on_member = on_member
        parser = self._expat
        parser.StartDoctypeDeclHandler = _reject_dtd
        parser.EndDoctypeDeclHandler = _reject_dtd
        parser.ExternalEntityRefHandler = _reject_dtd
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._data

    @staticmethod
    def _qname(name):
        if '|' in name:
            uri, local = name.split('|', 1)
            return ('{%s}%s' % (uri, local)) if uri else local
        return name

    def _start(self, name, attrs):
        tag = self._qname(name)
        fixed = {self._qname(key): value for key, value in attrs.items()}
        elem = self._builder.start(tag, fixed)
        depth = len(self._stack)
        self._stack.append(tag)
        if depth == 0:
            self._root = elem
            self._on_root(elem)
        if tag == WFS_MEMBER and self._member_depth is None:
            self._member_depth = depth

    def _end(self, name):
        tag = self._qname(name)
        elem = self._builder.end(tag)
        depth = len(self._stack) - 1
        self._stack.pop()
        if tag == WFS_MEMBER and self._member_depth == depth:
            self._member_depth = None
            # 成员已完整：立刻从根摘除，元素引用随队列存活，DOM保持有界
            root = self._root
            if root is not None and len(root) and root[-1] is elem:
                root.remove(elem)
            self._on_member(elem, root)

    def _data(self, text):
        self._builder.data(text)

    def feed(self, data):
        try:
            self._expat.Parse(data, False)
        except _expat.ExpatError as exc:
            raise ET.ParseError(str(exc)) from exc

    def finish(self):
        try:
            self._expat.Parse(b'', True)
        except _expat.ExpatError as exc:
            raise ET.ParseError(str(exc)) from exc
        return self._root


def local_name(tag):
    return tag.split('}', 1)[1] if tag.startswith('{') else tag


def namespace_of(tag):
    return tag[1:].split('}', 1)[0] if tag.startswith('{') else ''


def element_path(elem):
    """生成不带前缀的稳定路径，如 {urn:transit}Stop/{urn:transit}point。"""
    parts = []
    cursor = elem
    seen = 0
    while cursor is not None and seen < 64:
        parts.append(cursor.tag)
        cursor = _PARENTS.get(cursor)
        seen += 1
    return '/'.join(reversed(parts))


# C版Element不允许挂自定义属性，父子关系用弱引用字典旁路维护
_PARENTS = weakref.WeakKeyDictionary()


def _attach_parents(root):
    for parent in root.iter():
        for child in list(parent):
            _PARENTS[child] = parent


@dataclass
class Issue:
    code: str
    message: str
    path: str = ''
    gml_id: str | None = None
    snippet: str = ''


@dataclass
class FeatureRecord:
    feature_key: str                 # typeName + 来源稳定id（gml:id缺失时用序号占位）
    type_name: str
    gml_id: str | None
    properties: dict                # 简单属性；引用属性保留ref状态
    geometries: list
    issues: list = field(default_factory=list)
    source_id: str = ''
    response_sha256: str = ''
    page_index: int = 0
    member_index: int = 0
    raw_snippet: str = ''           # 该成员原XML片段，用于回到原响应
    outbound_refs: list = field(default_factory=list)


@dataclass
class CollectionHeader:
    number_matched: str | None
    number_returned: str | None
    next_url: str | None
    time_stamp: str | None
    srs_name: str | None            # 集合级srsName，几何未自带时的继承来源
    raw_attribs: dict


def _find_direct(elem, namespace, name):
    return elem.find('{%s}%s' % (namespace, name))


def _child_elements(elem):
    return [child for child in list(elem) if isinstance(child.tag, str)]


# ---------- 几何 ----------

def _resolve_srs_name(elem, collection_srs):
    cursor = elem
    while cursor is not None:
        name = cursor.get('srsName')
        if name:
            return name, element_path(cursor)
        cursor = _PARENTS.get(cursor)
    if collection_srs:
        return collection_srs, '$collection.srsName'
    return None, ''


def _pos_values(pos_elem, dimension_hint):
    values, issues = crs.parse_number_tokens(
        pos_elem.text or '', expected_dimension=dimension_hint,
        path=element_path(pos_elem))
    declared_dim = pos_elem.get('dimension')
    if declared_dim is not None:
        try:
            if int(declared_dim) != len(values):
                issues.append(crs.AxisIssue(
                    'dimension_mismatch',
                    'gml:pos dimension=%s 但实际有%d个数值' % (declared_dim, len(values)),
                    element_path(pos_elem)))
        except ValueError:
            issues.append(crs.AxisIssue('dimension_parse',
                                        'dimension属性无法解释: %r' % declared_dim,
                                        element_path(pos_elem)))
    return values, issues


def _interpret_position(values, srs_name, path):
    interpreted = crs.reorder_coordinates(values, srs_name, path=path)
    return interpreted


def _convert_point(pos_elem, srs_name, path):
    values, issues = _pos_values(pos_elem, None)
    interpreted = _interpret_position(values, srs_name, path)
    interpreted['issues'] = list(issues) + list(interpreted['issues'])
    if interpreted['status'] == 'converted':
        return {'type': 'Point', 'coordinates': interpreted['canonical'],
                'status': 'converted'}, interpreted, issues
    return ({'type': 'Point', 'coordinates': None, 'status': interpreted['status'],
             'original_coordinates': interpreted['original']}, interpreted, issues)


def _ring_points(ring_elem, srs_name):
    """解析环（LinearRing/Ring），返回 (点解释列表, 问题列表, 闭合状态)。"""
    points, issues, closed = [], [], None
    pos_list = _find_direct(ring_elem, GML_NS, 'posList')
    pos_elems = ring_elem.findall('{%s}pos' % GML_NS)
    if pos_list is not None:
        dim_hint = _dimension_of(pos_list)
        values, parse_issues = crs.parse_number_tokens(
            pos_list.text or '', expected_dimension=dim_hint,
            path=element_path(pos_list))
        issues.extend(parse_issues)
        dim = dim_hint or _guess_dimension(values)
        if dim:
            if len(values) % dim != 0:
                issues.append(crs.AxisIssue(
                    'dimension_mismatch',
                    'posList数值个数与维数不符: %d/%s' % (len(values), dim),
                    element_path(pos_list)))
            for offset in range(0, len(values) - dim + 1, dim):
                chunk = values[offset:offset + dim]
                interpreted = _interpret_position(chunk, srs_name, element_path(pos_list))
                issues.extend(interpreted['issues'])
                points.append(interpreted)
        closed_attr = pos_list.get('count')
        if closed_attr is not None:
            closed = closed_attr
    for pos_elem in pos_elems:
        values, parse_issues = _pos_values(pos_elem, None)
        issues.extend(parse_issues)
        interpreted = _interpret_position(values, srs_name, element_path(pos_elem))
        issues.extend(interpreted['issues'])
        points.append(interpreted)
    # 兼容老式 gml:coordinates（逗号分隔）
    legacy = _find_direct(ring_elem, GML_NS, 'coordinates')
    if legacy is not None and not points:
        points, legacy_issues, closed = _legacy_coordinates(legacy, srs_name)
        issues.extend(legacy_issues)
    return points, issues, closed


def _dimension_of(elem):
    for declared in (elem.get('srsDimension'), elem.get('dimension')):
        if declared is not None:
            try:
                return int(declared)
            except ValueError:
                continue
    return None


def _legacy_coordinates(coord_elem, srs_name):
    sep = coord_elem.get('cs', ',')
    decimal = coord_elem.get('decimal', '.')
    points, issues = [], []
    for tuple_text in (coord_elem.text or '').split():
        parts = tuple_text.split(sep)
        values = []
        for part in parts:
            try:
                values.append(float(part if decimal == '.' else part.replace(decimal, '.', 1)))
            except ValueError:
                issues.append(crs.AxisIssue('number_parse',
                                            '老式coordinates数值无法解释: %r' % part,
                                            element_path(coord_elem)))
        interpreted = _interpret_position(values, srs_name, element_path(coord_elem))
        issues.extend(interpreted['issues'])
        points.append(interpreted)
    return points, issues, None


def _interpreted_to_coords(interpreted):
    if interpreted['status'] == 'converted':
        return interpreted['canonical']
    return {'status': interpreted['status'], 'original': interpreted['original']}


def _check_ring_structure(points, issues, path):
    """环至少4点且首尾闭合；问题只记录不抛异常。"""
    if points and len(points) < 4:
        issues.append(crs.AxisIssue(
            'ring_too_few_points', '环只有%d个点，GML要求至少4个（含闭合点）' % len(points), path))
    if len(points) >= 2:
        first, last = points[0]['original'], points[-1]['original']
        if first != last:
            issues.append(crs.AxisIssue('ring_not_closed',
                                        '环首尾坐标不一致，结构可能无效', path))


def parse_geometry(geom_elem, collection_srs):
    """把一个GML几何元素转换为统一结构；任何子问题都收集在issues中。"""
    _attach_parents(geom_elem)
    srs_name, srs_path = _resolve_srs_name(geom_elem, collection_srs)
    issues = []
    name = local_name(geom_elem.tag)
    if srs_name is None:
        issues.append(crs.AxisIssue('missing_srs', '几何及其祖先均未声明srsName',
                                    element_path(geom_elem)))
        srs_name_for_parse = None
    else:
        srs_name_for_parse = srs_name

    if name == 'Point':
        pos = _find_direct(geom_elem, GML_NS, 'pos')
        if pos is None:
            issues.append(Issue('geometry_incomplete', 'Point缺少gml:pos',
                                element_path(geom_elem)))
            return {'type': 'Point', 'coordinates': None, 'status': 'invalid',
                    'srs_name': srs_name}, issues
        geometry, interpreted, pos_issues = _convert_point(pos, srs_name_for_parse,
                                                            element_path(pos))
        geometry['srs_name'] = srs_name
        geometry['crs_key'] = interpreted['crs_key']
        return geometry, issues + pos_issues

    if name in ('LineString',):
        return _parse_linestring(geom_elem, srs_name_for_parse, srs_name, issues)

    if name == 'Polygon':
        return _parse_polygon(geom_elem, srs_name_for_parse, srs_name, issues)

    if name in ('MultiPoint', 'MultiLineString', 'MultiSurface', 'MultiGeometry',
                'Surface', 'CompositeSurface'):
        return _parse_geometry_collection(geom_elem, srs_name, collection_srs, issues, name)

    issues.append(Issue('unsupported_geometry',
                        '未支持的几何类型gml:%s，保留原结构引用不转换' % name,
                        element_path(geom_elem)))
    return {'type': name, 'coordinates': None, 'status': 'unsupported_geometry',
            'srs_name': srs_name}, issues


def _parse_linestring(elem, srs_name_for_parse, srs_name, issues):
    coords = []
    pos_list = _find_direct(elem, GML_NS, 'posList')
    if pos_list is not None:
        dim = _dimension_of(pos_list)
        values, parse_issues = crs.parse_number_tokens(
            pos_list.text or '', expected_dimension=dim, path=element_path(pos_list))
        issues.extend(parse_issues)
        step = dim or _guess_dimension(values)
        if step and len(values) % step != 0:
            issues.append(crs.AxisIssue('dimension_mismatch',
                                        'posList数值个数%d不是维数%d的倍数' % (len(values), step),
                                        element_path(pos_list)))
        for offset in range(0, len(values) - step + 1, step):
            interpreted = _interpret_position(values[offset:offset + step],
                                              srs_name_for_parse, element_path(pos_list))
            issues.extend(interpreted['issues'])
            coords.append(_interpreted_to_coords(interpreted))
    for pos in elem.findall('{%s}pos' % GML_NS):
        values, parse_issues = _pos_values(pos, None)
        issues.extend(parse_issues)
        interpreted = _interpret_position(values, srs_name_for_parse, element_path(pos))
        issues.extend(interpreted['issues'])
        coords.append(_interpreted_to_coords(interpreted))
    if not coords:
        issues.append(Issue('geometry_incomplete', 'LineString没有任何坐标',
                            element_path(elem)))
    return {'type': 'LineString', 'coordinates': coords,
            'status': 'converted' if coords else 'invalid',
            'srs_name': srs_name}, issues


def _guess_dimension(values):
    for candidate in (2, 3):
        if len(values) % candidate == 0:
            return candidate
    return 0


def _parse_polygon(elem, srs_name_for_parse, srs_name, issues):
    exterior, interiors = [], []
    ext = elem.find('.//{%s}exterior' % GML_NS)
    if ext is None:
        issues.append(Issue('geometry_incomplete', 'Polygon缺少gml:exterior',
                            element_path(elem)))
    else:
        ring = _first_ring(ext)
        if ring is None:
            issues.append(Issue('geometry_incomplete', 'exterior缺少环元素',
                                element_path(ext)))
        else:
            points, ring_issues, _ = _ring_points(ring, srs_name_for_parse)
            issues.extend(ring_issues)
            _check_ring_structure(points, issues, element_path(ring))
            exterior = [_interpreted_to_coords(p) for p in points]
    for hole in elem.findall('.//{%s}interior' % GML_NS):
        ring = _first_ring(hole)
        if ring is None:
            issues.append(Issue('geometry_incomplete', 'interior缺少环元素',
                                element_path(hole)))
            continue
        points, ring_issues, _ = _ring_points(ring, srs_name_for_parse)
        issues.extend(ring_issues)
        _check_ring_structure(points, issues, element_path(ring))
        interiors.append([_interpreted_to_coords(p) for p in points])
    status = 'converted' if exterior else 'invalid'
    return {'type': 'Polygon', 'coordinates': [exterior] + interiors,
            'status': status, 'srs_name': srs_name}, issues


def _first_ring(container):
    for child in _child_elements(container):
        if local_name(child.tag) in _RING_TAGS:
            return child
    return None


def _parse_geometry_collection(elem, srs_name, collection_srs, issues, name):
    # MultiSurface: surfaceMembers -> Polygon/Surface；MultiPoint/MultiLineString
    # 用pointMembers/lineStringMember等；MultiGeometry可含任意成员几何。
    members = []
    for child in elem.iter():
        if child is elem:
            continue
        tag = local_name(child.tag)
        if tag in _GEOMETRY_TAGS and _is_direct_geometry_member(child, elem):
            sub_geometry, sub_issues = parse_geometry(child, collection_srs)
            issues.extend(sub_issues)
            members.append(sub_geometry)
    if not members:
        issues.append(Issue('geometry_incomplete',
                            '%s不包含任何成员几何' % name, element_path(elem)))
    mapped_type = {
        'MultiPoint': 'MultiPoint',
        'MultiLineString': 'MultiLineString',
        'MultiSurface': 'MultiPolygon',
        'Surface': 'MultiPolygon',
        'CompositeSurface': 'MultiPolygon',
        'MultiGeometry': 'GeometryCollection',
    }[name]
    if mapped_type == 'GeometryCollection':
        # 异构集合：保留每个成员的类型标签
        coordinates = members
    else:
        # 同质多部件：展开为统一坐标嵌套；成员状态异常时保留状态对象不丢点
        coordinates = [member['coordinates'] if member['status'] == 'converted'
                       else {'status': member['status'],
                             'original_type': member['type']}
                       for member in members]
    # Surface/CompositeSurface的patches内部是Polygon，已在上面收集
    return {'type': mapped_type, 'coordinates': coordinates,
            'status': 'converted' if members else 'invalid',
            'srs_name': srs_name}, issues


def _is_direct_geometry_member(candidate, root_geom):
    """避免把Polygon内部的Point等嵌套元素重复当成顶层成员。"""
    parent = _PARENTS.get(candidate)
    while parent is not None and parent is not root_geom:
        if local_name(parent.tag) in _GEOMETRY_TAGS or local_name(parent.tag) in _SURFACE_TAGS:
            return False
        parent = _PARENTS.get(parent)
    return parent is root_geom


# ---------- 要素与成员 ----------

def _ref_state(href):
    if href is None:
        return None
    if href.startswith('#'):
        return {'href': href, 'state': 'pending_local', 'resolved_from': None,
                'depth': None, 'cycles': []}
    return {'href': href, 'state': 'pending_remote', 'resolved_from': None,
            'depth': None, 'cycles': []}


def parse_feature(member_elem, *, source_id, response_sha256, page_index,
                  member_index, collection_srs, id_seen):
    """把单个wfs:member内容转换为FeatureRecord。"""
    feature_elem = None
    for child in _child_elements(member_elem):
        feature_elem = child
        break
    issues = []
    if feature_elem is None:
        return FeatureRecord(
            feature_key='%s#member-%d' % (source_id, member_index),
            type_name='', gml_id=None, properties={}, geometries=[],
            issues=[Issue('empty_member', 'wfs:member没有要素子元素',
                          element_path(member_elem))],
            source_id=source_id, response_sha256=response_sha256,
            page_index=page_index, member_index=member_index,
            raw_snippet=ET.tostring(member_elem, encoding='unicode')[:2000])

    type_name = feature_elem.tag
    gml_id = feature_elem.get(GML_ID)
    if gml_id is None:
        issues.append(Issue('missing_gml_id', '要素缺少gml:id，业务键退化为成员序号',
                            element_path(feature_elem)))
        feature_key = '%s:%s#member-%d' % (source_id, type_name, member_index)
    else:
        feature_key = '%s:%s/%s' % (source_id, type_name, gml_id)
        if gml_id in id_seen:
            issues.append(Issue('duplicate_gml_id',
                                'gml:id=%r在本响应内重复，业务键不唯一' % gml_id,
                                element_path(feature_elem)))
        id_seen.add(gml_id)

    record = FeatureRecord(
        feature_key=feature_key, type_name=type_name, gml_id=gml_id,
        properties={}, geometries=[], issues=issues,
        source_id=source_id, response_sha256=response_sha256,
        page_index=page_index, member_index=member_index,
        raw_snippet=ET.tostring(member_elem, encoding='unicode')[:4000])

    for prop in _child_elements(feature_elem):
        prop_name = prop.tag
        path = element_path(prop)
        href = prop.get(XLINK_HREF)
        nil = prop.get(GML_NIL)
        if href is not None:
            state = _ref_state(href)
            state['property'] = prop_name
            record.outbound_refs.append(state)
            record.properties[prop_name] = {'$ref': state}
            continue
        if nil == 'true':
            record.properties[prop_name] = None
            continue
        child_geom = _first_geometry(prop)
        if child_geom is not None:
            geometry, geom_issues = parse_geometry(child_geom, collection_srs)
            geometry['property'] = prop_name
            for item in geom_issues:
                record.issues.append(_to_issue(item, gml_id))
            record.geometries.append(geometry)
            continue
        text_value = _text_property(prop)
        if text_value is not None:
            record.properties[prop_name] = text_value
        else:
            record.issues.append(Issue('uninterpretable_property',
                                       '属性既非文本、几何也非引用，保留未解释状态',
                                       path, gml_id))
    return record


def _first_geometry(prop):
    for child in prop.iter():
        if child is prop:
            continue
        if local_name(child.tag) in _GEOMETRY_TAGS:
            return child
    return None


def _text_property(prop):
    children = _child_elements(prop)
    if not children:
        text = (prop.text or '').strip()
        return text if text else None
    # 带简单属性的包装（如gml:description）：只在全部为文本叶子时接受
    if all(not _child_elements(child) for child in children):
        merged = ' '.join((child.text or '').strip() for child in children).strip()
        return merged or None
    return None


def _to_issue(item, gml_id):
    if isinstance(item, Issue):
        if not item.gml_id:
            item.gml_id = gml_id
        return item
    return Issue(code=item.code, message=item.message, path=item.path, gml_id=gml_id)


# ---------- 流式集合遍历 ----------

def iter_feature_collection(stream, *, source_id, response_sha256_getter,
                            page_index=0, root_holder=None):
    """增量解析WFS FeatureCollection。

    参数stream为类文件对象（source.StreamedResponse.stream）。
    产出 (CollectionHeader, 生成器)：生成器逐个给出FeatureRecord，
    每个成员处理完即清除其DOM节点，内存只保留当前成员。
    root_holder若非None，根元素会放入root_holder['root']，供读取wfs:next等
    集合级链接（根元素本身很小）。
    """
    header_box = {}
    member_queue = []

    def on_root(elem):
        if elem.tag != WFS_FEATURE_COLLECTION:
            raise GmlFormatError('根元素不是wfs:FeatureCollection: %r' % elem.tag)
        if root_holder is not None:
            root_holder['root'] = elem
        header_box['header'] = CollectionHeader(
            number_matched=elem.get('numberMatched'),
            number_returned=elem.get('numberReturned'),
            next_url=None,
            time_stamp=elem.get('timeStamp'),
            srs_name=elem.get('srsName'),
            raw_attribs=dict(elem.attrib))

    parser = _StreamingMemberParser(on_root, lambda member, root:
                                    member_queue.append((member, root)))

    # 先喂一小段以拿到根元素与集合属性；小primer避免在首块里积压大量成员DOM
    primer = stream.read(4096)
    if not primer:
        raise GmlFormatError('响应为空')
    parser.feed(primer)
    if 'header' not in header_box:
        # 根可能在更大的前缀里才闭合（属性很多），继续读到根出现
        while 'header' not in header_box:
            chunk = stream.read(4096)
            if not chunk:
                parser.finish()
                raise GmlFormatError('响应中没有wfs:FeatureCollection根元素')
            parser.feed(chunk)

    def generator_with_primer():
        member_index = 0
        id_seen = set()

        def drain():
            nonlocal member_index
            while member_queue:
                member, root = member_queue.pop(0)
                _attach_parents(member)
                record = parse_feature(
                    member, source_id=source_id,
                    response_sha256=response_sha256_getter(),
                    page_index=page_index, member_index=member_index,
                    collection_srs=header_box['header'].srs_name,
                    id_seen=id_seen)
                member_index += 1
                yield record
                # 成员按文档顺序入队；根的直接子节点中已处理成员排在最前
                direct = list(root)
                if direct and direct[0] is member:
                    root.remove(member)
                elif member in direct:
                    root.remove(member)

        yield from drain()
        while True:
            chunk = stream.read(16384)
            if not chunk:
                break
            parser.feed(chunk)
            # 每块解析后立刻排空，根下滞留成员不超过一个读块的产出
            yield from drain()
        root = parser.finish()
        if root is None or root.tag != WFS_FEATURE_COLLECTION:
            raise GmlFormatError('响应中没有wfs:FeatureCollection根元素')
        yield from drain()

    return header_box['header'], generator_with_primer()


def parse_exception_report(body_bytes):
    """识别HTTP 200内的OWS ExceptionReport；非异常报告返回None。"""
    try:
        root = ET.fromstring(body_bytes, parser=safe_xml_parser())
    except ET.ParseError:
        return None
    local = local_name(root.tag)
    if local != 'ExceptionReport':
        return None
    ows_ns = namespace_of(root.tag)
    exceptions = []
    for exc in root.findall('{%s}Exception' % ows_ns):
        text_nodes = [node.text.strip() for node in
                      exc.findall('{%s}ExceptionText' % ows_ns) if node.text]
        exceptions.append({
            'exception_code': exc.get('exceptionCode'),
            'locator': exc.get('locator'),
            'texts': text_nodes,
        })
    return {
        'report_namespace': ows_ns,
        'version': root.get('version'),
        'exceptions': exceptions,
    }
