"""CRS标识、轴序、维数与单位的显式解释。

口径（见docs/source-contract.md）：
- EPSG:4326正式轴序为纬/经（lat, lon），CRS84为经/纬（lon, lat）；
  二者数字表示相似但绝不能按同序处理。
- 只转换注册表明确支持的CRS，其他CRS保留未转换状态并携带原srsName。
- 输出统一为经/纬（含高程时为经/纬/高），同时保留原坐标序列与原序。
"""
import re
from dataclasses import dataclass

# 常见srsName形态：
#   EPSG:4326 / urn:ogc:def:crs:EPSG::4326 /
#   http://www.opengis.net/def/crs/EPSG/0/4326 /
#   urn:ogc:def:crs:OGC:1.3:CRS84 / OGC:CRS84
_URN_RE = re.compile(r'^urn:ogc:def:crs:([^:]+):(?:[^:]*):(.+)$', re.IGNORECASE)
_URL_RE = re.compile(r'^https?://www\.opengis\.net/def/crs/([^/]+)/[^/]+/(.+)$', re.IGNORECASE)
_SHORT_RE = re.compile(r'^([A-Za-z][A-Za-z0-9]*):(.+)$')

# 轴方向代号：easting=东向（经度方向），northing=北向（纬度方向），up=高程
AXIS_LON = 'easting'
AXIS_LAT = 'northing'
AXIS_UP = 'up'
AXIS_OTHER = 'other'


@dataclass(frozen=True)
class CrsInfo:
    key: str                       # 归一化键，如 'EPSG:4326'、'OGC:CRS84'
    kind: str                      # 'geographic' | 'projected' | 'vertical' | 'unknown'
    axes: tuple                    # 轴方向，如 (AXIS_LAT, AXIS_LON)
    units: tuple                   # 每轴单位，如 ('degree','degree','metre')
    dimension: int = 0

    def __post_init__(self):
        if not self.dimension:
            object.__setattr__(self, 'dimension', len(self.axes))


@dataclass
class AxisIssue:
    """坐标序列解释时发现的问题；记录不删除，要素仍保留。"""
    code: str
    message: str
    path: str = ''


# 注册表只放入来源契约明确支持的CRS。维度用2/3两套键分别登记。
_REGISTRY = {
    'EPSG:4326': CrsInfo('EPSG:4326', 'geographic',
                         (AXIS_LAT, AXIS_LON), ('degree', 'degree')),
    'EPSG:4979': CrsInfo('EPSG:4979', 'geographic',
                         (AXIS_LAT, AXIS_LON, AXIS_UP),
                         ('degree', 'degree', 'metre')),
    'OGC:CRS84': CrsInfo('OGC:CRS84', 'geographic',
                         (AXIS_LON, AXIS_LAT), ('degree', 'degree')),
    'OGC:CRS84h': CrsInfo('OGC:CRS84h', 'geographic',
                          (AXIS_LON, AXIS_LAT, AXIS_UP),
                          ('degree', 'degree', 'metre')),
}


def normalize_srs_name(srs_name):
    """把各种srsName写法归一为 '权威:代码' 键；无法识别返回None。"""
    if not srs_name or not isinstance(srs_name, str):
        return None
    text = srs_name.strip()
    if '#' in text:
        text = text.split('#', 1)[0] or text
    text = text.rstrip('/')
    match = _URN_RE.match(text)
    if match:
        authority, code = match.groups()
        return authority.upper() + ':' + code
    match = _URL_RE.match(text)
    if match:
        authority, code = match.groups()
        return authority.upper() + ':' + code
    match = _SHORT_RE.match(text)
    if match:
        authority, code = match.groups()
        authority = authority.upper()
        if authority == 'OGC' and code.upper() == 'CRS84':
            return 'OGC:CRS84'
        return authority + ':' + code
    return None


def register_crs(key, info):
    """登记调用方明确授权使用的额外CRS；不允许覆盖内置契约CRS。"""
    if key in _REGISTRY:
        raise ValueError('内置CRS不可覆盖: ' + key)
    if not isinstance(info, CrsInfo):
        raise ValueError('CrsInfo结构无效')
    _REGISTRY[key] = info


def lookup(srs_name):
    """按原始srsName查注册表；返回 (归一化键, CrsInfo|None)。"""
    key = normalize_srs_name(srs_name)
    if key is None:
        return None, None
    return key, _REGISTRY.get(key)


def _target_permutation(info):
    """返回把info轴序重排为 (经度,纬度[,高程]) 的索引序列。"""
    wanted = [AXIS_LON, AXIS_LAT] + ([AXIS_UP] if info.dimension == 3 else [])
    permutation = []
    for axis in wanted:
        try:
            permutation.append(info.axes.index(axis))
        except ValueError:
            return None
    return tuple(permutation)


def reorder_coordinates(values, srs_name, *, path=''):
    """按CRS契约解释一组坐标。

    返回dict：
      status: converted | unsupported_crs | invalid
      canonical: 统一经/纬（/高）序列，仅converted时存在
      original:  原始数值序列（始终保留）
      crs_key / srs_name / units / issues
    """
    original = list(values)
    key, info = lookup(srs_name)
    result = {
        'status': 'invalid',
        'original': original,
        'srs_name': srs_name,
        'crs_key': key,
        'canonical': None,
        'units': None,
        'issues': [],
    }
    if info is None:
        result['status'] = 'unsupported_crs'
        result['issues'].append(AxisIssue(
            'unsupported_crs',
            'CRS不在授权注册表，保留原坐标不做轴序解释: %r' % (srs_name,),
            path))
        return result
    result['units'] = list(info.units)
    if len(original) != info.dimension:
        result['status'] = 'invalid'
        result['issues'].append(AxisIssue(
            'dimension_mismatch',
            '坐标维数%d与CRS %s 声明维数%d不一致' % (len(original), info.key, info.dimension),
            path))
        return result
    permutation = _target_permutation(info)
    if permutation is None:
        result['status'] = 'unsupported_crs'
        result['issues'].append(AxisIssue(
            'axis_not_mappable',
            'CRS %s 轴方向无法映射到经/纬序列' % info.key,
            path))
        return result
    canonical = [original[index] for index in permutation]
    result['canonical'] = canonical
    result['status'] = 'converted'
    lon, lat = canonical[0], canonical[1]
    # 经/纬合理性检查：若经度落在纬度带、纬度落在经度带，强烈提示轴序被来源写反。
    if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
        result['issues'].append(AxisIssue(
            'axis_range_suspect',
            '转换后经度%.6f纬度%.6f超出地理范围，来源轴序声明可能与数据不符' % (lon, lat),
            path))
    elif abs(lon) > 90.0 >= abs(lat) and info.axes[:2] == (AXIS_LAT, AXIS_LON):
        # 声明纬/经，但数值形态更像经/纬：记录冲突而不是静默改写
        result['issues'].append(AxisIssue(
            'axis_order_conflict',
            '声明%s为纬/经，但数值形态像经/纬，保留按契约转换的结果并标记冲突' % info.key,
            path))
    return result


def parse_number_tokens(text, *, expected_dimension=None, path=''):
    """解析gml:pos/posList空白分隔的数值串，返回 (数值列表, 问题列表)。"""
    issues = []
    tokens = text.split()
    values = []
    for token in tokens:
        try:
            values.append(float(token))
        except ValueError:
            issues.append(AxisIssue('number_parse', '无法解释的坐标数值: %r' % token, path))
    if expected_dimension is not None and values and len(values) % expected_dimension != 0:
        issues.append(AxisIssue(
            'dimension_mismatch',
            'posList数值个数%d不是维数%d的整数倍' % (len(values), expected_dimension),
            path))
    return values, issues
