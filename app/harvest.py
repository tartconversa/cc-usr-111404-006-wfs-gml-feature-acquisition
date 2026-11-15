"""WFS2.0采集编排：能力确认 -> 类型描述 -> 流式分页采集 -> 引用解析 -> 报告。

完整性口径：
- 只有来源显式声明稳定排序/快照且本客户端全程保持过滤与排序不变时，才可能给出
  snapshot_complete；否则只报告bounded_observation（有界观察）及冲突。
- numberMatched=unknown、跨页numberMatched变化、成员数与numberReturned不符等
  都作为conflict记录，不用numberMatched单独证明完整。
- 每页独立sha256与要素级issue保证字段/几何异常可回到具体要素和原响应。
"""
import datetime as _dt
from dataclasses import dataclass, field, asdict

from .gmlstream import iter_feature_collection
from .refs import ReferencePolicy, ReferenceResolver
from .wfs import WfsClient, WfsException, extract_next_link


@dataclass
class HarvestConfig:
    endpoint: str
    source_id: str
    type_name: str
    count: int = 100
    max_pages: int = 10
    max_bytes_per_response: int = 8 * 1024 * 1024
    filter_xml: str | None = None
    sort_by: str | None = None
    output_format: str | None = None
    method: str = 'GET'
    # 稳定性由调用方在核对来源契约后显式授权；默认不信任
    assume_stable_snapshot: bool = False
    reference_policy: ReferencePolicy = field(default_factory=ReferencePolicy)
    extra_params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    # 远程引用抓取器：fetcher(url) -> (source_id, {gml_id: FeatureRecord})
    remote_fetcher: object = None


@dataclass
class PageReport:
    page_index: int
    url: str
    method: str
    status: int
    response_sha256: str
    bytes_read: int
    number_matched: str | None
    number_returned: str | None
    next_url: str | None
    member_count: int
    issue_count: int


@dataclass
class HarvestReport:
    source_id: str
    endpoint: str
    type_name: str
    started_at: str
    finished_at: str | None
    observation_scope: str           # bounded_observation | snapshot_complete
    snapshot_claim: str              # 依据说明
    pages: list
    total_members: int
    unresolved_refs: int
    conflicts: list
    capabilities_summary: dict
    feature_type: dict
    request_log: list
    stopped_reason: str | None = None


def _utc_now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class _PageBoundary:
    page_index: int
    url: str
    method: str
    status: int
    sha: str
    bytes_read: int
    number_matched: str | None
    number_returned: str | None
    next_url: str | None
    member_count: int
    issue_count: int
    returned_int: int | None


@dataclass
class _Stop:
    reason: str


def _as_int(text):
    if text is None or text == 'unknown':
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _stability_authorized(capabilities, config):
    if config.assume_stable_snapshot and config.sort_by:
        return True
    if capabilities.supports_stable_sort() and config.sort_by:
        return True
    return False


def harvest(config):
    """执行一轮采集，返回 (要素记录列表, HarvestReport)。"""
    started = _utc_now()
    client = WfsClient(config.endpoint, config.source_id,
                       max_bytes=config.max_bytes_per_response,
                       extra_params=config.extra_params,
                       headers=config.headers)
    conflicts = []
    capabilities = client.get_capabilities()
    if config.type_name not in capabilities.feature_type_names:
        # 名称可能带前缀差异，做本地名兜底核对
        local_names = [name.split(':')[-1] for name in capabilities.feature_type_names]
        if config.type_name.split(':')[-1] not in local_names:
            raise WfsException('Capabilities未声明可取类型 %r' % config.type_name)
        conflicts.append('type_name前缀与Capabilities声明不完全一致，按本地名匹配: %s'
                         % config.type_name)
    description = client.describe_feature_type(config.type_name,
                                               capabilities=capabilities)
    if description.is_abstract:
        conflicts.append('类型 %s 被声明为abstract，实例可能来自替换类型' % config.type_name)

    records, page_reports = [], []
    stop_reason = None
    matched_values = []
    for event in _drive_pages(client, config, capabilities):
        if isinstance(event, _Stop):
            stop_reason = event.reason
            break
        if isinstance(event, _PageBoundary):
            page_reports.append(PageReport(
                event.page_index, event.url, event.method, event.status,
                event.sha, event.bytes_read, event.number_matched,
                event.number_returned, event.next_url, event.member_count,
                event.issue_count))
            if event.number_matched is not None:
                matched_values.append(event.number_matched)
            if event.returned_int is not None and \
                    event.returned_int != event.member_count:
                conflicts.append(
                    '第%d页numberReturned=%d但实际成员%d'
                    % (event.page_index, event.returned_int, event.member_count))
            continue
        records.append(event)

    # 引用解析（采集完整批后统一做，前向片段引用也能命中）
    resolver = ReferenceResolver(
        config.reference_policy, source_id=config.source_id,
        base_url=config.endpoint, fetcher=config.remote_fetcher)
    for record in records:
        resolver.register_local(record)
    resolver.resolve_all(records)
    unresolved = sum(1 for record in records for state in record.outbound_refs
                     if state['state'] not in ('resolved_local', 'resolved_remote'))

    if len(set(matched_values)) > 1:
        conflicts.append('跨页numberMatched发生变化: %s，来源不具备稳定快照'
                         % sorted(set(matched_values)))
    if matched_values and matched_values[0] == 'unknown':
        conflicts.append('来源声明numberMatched=unknown，无法据此证明完整')

    observation, claim = _observation_scope(config, capabilities, stop_reason,
                                            page_reports, conflicts)
    report = HarvestReport(
        source_id=config.source_id, endpoint=config.endpoint,
        type_name=config.type_name, started_at=started,
        finished_at=_utc_now(), observation_scope=observation,
        snapshot_claim=claim,
        pages=[asdict(page) for page in page_reports],
        total_members=len(records), unresolved_refs=unresolved,
        conflicts=conflicts,
        capabilities_summary={
            'version': capabilities.version,
            'feature_type_names': list(capabilities.feature_type_names),
            'paging': capabilities.supports_paging(),
            'stable_sort_declared': capabilities.supports_stable_sort(),
            'count_default': capabilities.default_count,
            'output_formats': list(capabilities.output_formats),
        },
        feature_type={
            'type_name': description.type_name,
            'is_abstract': description.is_abstract,
            'properties': [list(item) for item in description.properties],
            'geometry_properties': list(description.geometry_properties),
        },
        request_log=client.request_log,
        stopped_reason=stop_reason)
    return records, report


def _drive_pages(client, config, capabilities):
    """与_iter_pages等价的内联驱动，保留根元素以读取next链接。"""
    page_request = client.build_page_request(
        config.type_name, count=config.count, filter_xml=config.filter_xml,
        sort_by=config.sort_by, output_format=config.output_format,
        method=config.method)
    for page_index in range(config.max_pages):
        streamed = client.open_page(page_request)
        root_holder = {}
        try:
            header, members = iter_feature_collection(
                streamed.stream, source_id=config.source_id,
                response_sha256_getter=lambda: None, page_index=page_index,
                root_holder=root_holder)
            batch = []
            for record in members:
                batch.append(record)
                yield record
        finally:
            streamed.close()
        client.mark_page_completed(streamed)
        sha = streamed.sha256
        for record in batch:
            record.response_sha256 = sha
        root = root_holder.get('root')
        next_url = extract_next_link(root) if root is not None else None
        returned = _as_int(header.number_returned)
        yield _PageBoundary(
            page_index=page_index, url=streamed.url, method=page_request.method,
            status=streamed.status, sha=sha, bytes_read=streamed.bytes_read,
            number_matched=header.number_matched,
            number_returned=header.number_returned, next_url=next_url,
            member_count=len(batch),
            issue_count=sum(len(r.issues) for r in batch),
            returned_int=returned)

        if not batch:
            yield _Stop('empty_page')
            return
        if returned is not None and returned != len(batch):
            yield _Stop('number_returned_conflict')
            return
        if next_url:
            page_request = client.follow_next(next_url, page_request)
            continue
        if (capabilities.supports_paging() and config.count
                and len(batch) == config.count and page_index + 1 < config.max_pages):
            if not _stability_authorized(capabilities, config):
                yield _Stop('no_stable_order_for_offset')
                return
            page_request = client.build_page_request(
                config.type_name, count=config.count,
                filter_xml=config.filter_xml, sort_by=config.sort_by,
                output_format=config.output_format, method=config.method,
                start_index=page_request.start_index + len(batch))
            continue
        yield _Stop('no_next_link')
        return
    yield _Stop('max_pages_reached')


def _observation_scope(config, capabilities, stop_reason, pages, conflicts):
    stable = _stability_authorized(capabilities, config)
    if conflicts:
        return ('bounded_observation',
                '存在冲突或来源口径变化，停止原因=%s，不能声明完整快照' % stop_reason)
    if stop_reason == 'no_next_link' and stable:
        return ('snapshot_complete',
                '来源声明稳定排序且调用方授权快照口径；过滤=%s 排序=%s 全程不变'
                % (bool(config.filter_xml), config.sort_by))
    if stop_reason == 'no_next_link':
        return ('bounded_observation',
                '来源未声明稳定排序，末页无next只表示当前观察结束，不承诺时点快照')
    if stop_reason in ('max_pages_reached', 'no_stable_order_for_offset'):
        return ('bounded_observation',
                '分页在有界页数内停止（%s），仅覆盖已观察页面，不保证未读部分'
                % stop_reason)
    return ('bounded_observation',
            '停止原因=%s；存在冲突或异常，不能声明完整快照' % stop_reason)
