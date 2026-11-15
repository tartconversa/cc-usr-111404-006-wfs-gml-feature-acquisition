"""xlink引用的受控解析：缓存、白名单、深度与环状态。

规则（见docs/source-contract.md）：
- 只解析本响应内片段引用，以及配置白名单授权来源内的对象；
- 禁止递归抓取任意地址：未授权、超深度、循环引用都保留状态而不是抛掉；
- 同一对象只抓取一次（按规范化URL+片段缓存），跨来源引用显式标记；
- 不解析file:、data:等非HTTP(S)方案。
"""
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit


@dataclass
class ReferencePolicy:
    # 允许发起远程抓取的来源前缀（完整origin或前缀），空集合表示完全禁止远程
    allowed_origins: tuple = ()
    max_depth: int = 3
    timeout: int = 5

    def allows(self, url):
        if self.max_depth < 0:
            return False
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https'):
            return False
        if not parts.netloc:
            return False
        return any(url.startswith(prefix) for prefix in self.allowed_origins)


@dataclass
class CacheEntry:
    key: str                        # 规范化URL（含片段）
    status: str                     # resolved_local | resolved_remote |
                                    # forbidden | unparsed | cycle |
                                    # too_deep | bad_scheme | not_found
    target: object = None
    source_id: str | None = None
    depth: int | None = None
    cycles: tuple = ()
    note: str = ''


class ReferenceResolver:
    """收集阶段先登记本地gml:id，结束后统一解析，前向/后向片段引用都能命中。

    链chain记录当前引用路径上已访问的要素feature_key，用于环检测；深度按远程
    跳转次数计。缓存命中时若目标已在当前链上，判定为cycle。
    """

    def __init__(self, policy, *, source_id, base_url, fetcher=None):
        self.policy = policy
        self.source_id = source_id
        self.base_url = base_url
        self._fetcher = fetcher      # fetcher(url) -> (source_id, 要素映射)
        self._local_index = {}       # gml:id -> FeatureRecord
        self._cache = {}             # 规范化href -> CacheEntry
        self.remote_fetches = []

    def register_local(self, record):
        if record.gml_id:
            self._local_index[record.gml_id] = record

    @staticmethod
    def normalize(href, base_url):
        if href.startswith('#'):
            return href
        return urljoin(base_url, href)

    def resolve_all(self, records):
        """对一批要素记录中的全部$ref做受控解析，状态写回引用字典。"""
        for record in records:
            for state in record.outbound_refs:
                self._resolve(state, chain=())
        return records

    def _resolve(self, state, chain):
        href = state['href']
        normalized = self.normalize(href, self.base_url)
        state['normalized'] = normalized
        depth = len(chain)

        cached = self._cache.get(normalized)
        if cached is not None:
            # 同一对象已解析过：直接复用结论；若它正在当前链上则是环
            if cached.target is not None and \
                    getattr(cached.target, 'feature_key', None) in chain:
                state.update(state='cycle',
                             cycles=list(chain) + [cached.target.feature_key],
                             depth=depth,
                             note='引用形成循环，保留状态不再深入',
                             resolved_from=cached.source_id)
                return
            state.update(state=cached.status, resolved_from=cached.source_id,
                         depth=cached.depth, cycles=list(cached.cycles),
                         note=cached.note)
            if cached.target is not None:
                state['target_feature_key'] = cached.target.feature_key
            return

        if href.startswith('#'):
            fragment = href[1:]
            target = self._local_index.get(fragment)
            if target is None:
                entry = CacheEntry(normalized, 'unparsed', depth=depth,
                                   note='本响应内找不到gml:id=%r' % fragment)
                self._cache[normalized] = entry
                self._apply(state, entry)
                return
            if target.feature_key in chain:
                entry = CacheEntry(
                    normalized, 'cycle', target, self.source_id, depth,
                    tuple(chain) + (target.feature_key,),
                    '引用形成循环，保留状态不再深入')
                self._cache[normalized] = entry
                self._apply(state, entry)
                return
            entry = CacheEntry(normalized, 'resolved_local', target,
                               self.source_id, depth)
            self._cache[normalized] = entry
            # 沿目标要素的出站引用继续传递链，使多跳环可被识别
            cycle = self._descend(target, chain + (target.feature_key,))
            if cycle:
                entry.status = 'cycle'
                entry.cycles = tuple(cycle)
                entry.note = '引用形成循环，保留状态不再深入'
            self._apply(state, entry)
            return

        parts = urlsplit(normalized)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            entry = CacheEntry(normalized, 'bad_scheme', depth=depth,
                               note='非HTTP(S)引用，按契约不抓取: %r' % href)
            self._cache[normalized] = entry
            self._apply(state, entry)
            return

        if depth >= self.policy.max_depth:
            entry = CacheEntry(normalized, 'too_deep', depth=depth,
                               note='超过最大引用深度%d' % self.policy.max_depth)
            self._cache[normalized] = entry
            self._apply(state, entry)
            return

        if not self.policy.allows(normalized):
            entry = CacheEntry(normalized, 'forbidden', depth=depth,
                               note='目标来源不在授权白名单，不递归抓取')
            self._cache[normalized] = entry
            self._apply(state, entry)
            return

        entry = self._fetch_remote(normalized, depth)
        self._cache[normalized] = entry
        self._apply(state, entry)

    def _descend(self, target, chain):
        """沿本地目标要素的出站引用继续解析；发现环时返回环链，否则返回None。"""
        for outbound in getattr(target, 'outbound_refs', []):
            child = dict(outbound)
            self._resolve(child, chain)
            outbound.update(child)
            if child.get('state') == 'cycle':
                return child.get('cycles')
        return None

    def _fetch_remote(self, normalized, depth):
        if self._fetcher is None:
            return CacheEntry(normalized, 'forbidden', depth=depth,
                              note='未配置远程抓取器')
        bare_url, fragment = urldefrag(normalized)
        try:
            remote_source_id, remote_index = self._fetcher(bare_url)
        except Exception as exc:  # 网络/解析异常保留为未解析，不中断整批采集
            return CacheEntry(normalized, 'unparsed', depth=depth,
                              note='远程对象读取失败: %s' % exc)
        self.remote_fetches.append({'url': bare_url, 'source_id': remote_source_id,
                                    'depth': depth})
        target = remote_index.get(fragment) if fragment else None
        if fragment and target is None:
            return CacheEntry(normalized, 'not_found', depth=depth,
                              source_id=remote_source_id,
                              note='授权来源内找不到片段gml:id=%r' % fragment)
        cross = 'cross_source' if remote_source_id != self.source_id else 'same_source'
        return CacheEntry(normalized, 'resolved_remote', target,
                          remote_source_id, depth, note=cross)

    @staticmethod
    def _apply(state, entry):
        state['state'] = entry.status
        state['resolved_from'] = entry.source_id
        state['depth'] = entry.depth
        state['cycles'] = list(entry.cycles)
        if entry.note:
            state['note'] = entry.note
        if entry.target is not None:
            state['target_feature_key'] = entry.target.feature_key

    @property
    def cache_snapshot(self):
        return {key: {'status': value.status, 'source_id': value.source_id,
                      'depth': value.depth, 'cycles': list(value.cycles),
                      'note': value.note}
                for key, value in self._cache.items()}
