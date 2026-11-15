"""核对xlink引用缓存：本地解析、环、未解析、白名单、深度与跨来源状态。"""
import unittest
from dataclasses import dataclass

from app.refs import ReferencePolicy, ReferenceResolver


@dataclass
class FakeRecord:
    gml_id: str
    feature_key: str
    outbound_refs: list = None

    def __post_init__(self):
        if self.outbound_refs is None:
            self.outbound_refs = []


def record(gml_id, source='src'):
    return FakeRecord(gml_id, '%s:%s' % (source, gml_id))


def add_ref(rec, href):
    state = {'href': href, 'state': 'pending', 'resolved_from': None,
             'depth': None, 'cycles': []}
    rec.outbound_refs = getattr(rec, 'outbound_refs', [])
    rec.outbound_refs.append(state)
    return state


class LocalReferenceTests(unittest.TestCase):
    def setUp(self):
        self.resolver = ReferenceResolver(
            ReferencePolicy(), source_id='src',
            base_url='http://example.test/wfs')

    def test_local_fragment_resolved_forward_and_backward(self):
        # 无环引用：a->b，c->a；登记顺序与引用方向无关，都应解析
        a, b, c = record('a'), record('b'), record('c')
        add_ref(a, '#b')
        add_ref(c, '#a')
        for item in (a, b, c):
            self.resolver.register_local(item)
        self.resolver.resolve_all([a, b, c])
        self.assertEqual(a.outbound_refs[0]['state'], 'resolved_local')
        self.assertEqual(a.outbound_refs[0]['target_feature_key'], 'src:b')
        self.assertEqual(c.outbound_refs[0]['state'], 'resolved_local')
        self.assertEqual(c.outbound_refs[0]['target_feature_key'], 'src:a')

    def test_unparsed_local_fragment_kept(self):
        a = record('a')
        add_ref(a, '#missing')
        self.resolver.register_local(a)
        self.resolver.resolve_all([a])
        state = a.outbound_refs[0]
        self.assertEqual(state['state'], 'unparsed')
        self.assertIn('missing', state['note'])

    def test_cycle_is_marked_not_recursed_forever(self):
        # a->b->c->a 三跳环，必须被标记为cycle且解析终止
        a, b, c = record('a'), record('b'), record('c')
        add_ref(a, '#b')
        add_ref(b, '#c')
        add_ref(c, '#a')
        for item in (a, b, c):
            self.resolver.register_local(item)
        self.resolver.resolve_all([a, b, c])
        states = {r.gml_id: r.outbound_refs[0]['state'] for r in (a, b, c)}
        self.assertIn('cycle', states.values())
        # 环上的引用都有终态且不抛异常、不无限递归
        self.assertTrue(all(s in ('resolved_local', 'cycle') for s in states.values()))
        cyclic = next(r for r in (a, b, c)
                      if r.outbound_refs[0]['state'] == 'cycle')
        self.assertTrue(cyclic.outbound_refs[0]['cycles'])

    def test_two_node_self_cycle(self):
        a, b = record('a'), record('b')
        add_ref(a, '#a')  # 自引用
        self.resolver.register_local(a)
        self.resolver.resolve_all([a])
        self.assertEqual(a.outbound_refs[0]['state'], 'cycle')
        self.assertIn('src:a', a.outbound_refs[0]['cycles'])

    def test_cache_deduplicates_same_href(self):
        a, b, target = record('a'), record('b'), record('shared')
        add_ref(a, '#shared')
        add_ref(b, '#shared')
        for item in (a, b, target):
            self.resolver.register_local(item)
        self.resolver.resolve_all([a, b])
        self.assertEqual(len(self.resolver.cache_snapshot), 1)


class RemotePolicyTests(unittest.TestCase):
    def test_remote_forbidden_by_default_no_fetch(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return 'other', {}

        resolver = ReferenceResolver(
            ReferencePolicy(allowed_origins=()), source_id='src',
            base_url='http://example.test/wfs', fetcher=fetcher)
        a = record('a')
        add_ref(a, 'http://other.test/obj#x')
        resolver.register_local(a)
        resolver.resolve_all([a])
        self.assertEqual(a.outbound_refs[0]['state'], 'forbidden')
        self.assertEqual(calls, [])

    def test_remote_allowed_and_cross_source_marked(self):
        other = record('x', source='other')

        def fetcher(url):
            return 'other', {'x': other}

        resolver = ReferenceResolver(
            ReferencePolicy(allowed_origins=('http://other.test',)),
            source_id='src', base_url='http://example.test/wfs',
            fetcher=fetcher)
        a = record('a')
        add_ref(a, 'http://other.test/obj#x')
        resolver.register_local(a)
        resolver.resolve_all([a])
        state = a.outbound_refs[0]
        self.assertEqual(state['state'], 'resolved_remote')
        self.assertEqual(state['resolved_from'], 'other')
        self.assertEqual(state['note'], 'cross_source')
        self.assertEqual(len(resolver.remote_fetches), 1)

    def test_depth_limit_stops_recursion(self):
        def fetcher(url):
            raise AssertionError('不应发起抓取')

        resolver = ReferenceResolver(
            ReferencePolicy(allowed_origins=('http://other.test',), max_depth=0),
            source_id='src', base_url='http://example.test/wfs',
            fetcher=fetcher)
        a = record('a')
        add_ref(a, 'http://other.test/obj#x')
        resolver.register_local(a)
        resolver.resolve_all([a])
        self.assertEqual(a.outbound_refs[0]['state'], 'too_deep')

    def test_bad_scheme_rejected(self):
        resolver = ReferenceResolver(
            ReferencePolicy(allowed_origins=('file://',)),
            source_id='src', base_url='http://example.test/wfs',
            fetcher=lambda url: ('x', {}))
        a = record('a')
        add_ref(a, 'file:///etc/passwd')
        resolver.register_local(a)
        resolver.resolve_all([a])
        self.assertEqual(a.outbound_refs[0]['state'], 'bad_scheme')

    def test_remote_failure_is_unparsed_not_fatal(self):
        def fetcher(url):
            raise OSError('连接被拒绝')

        resolver = ReferenceResolver(
            ReferencePolicy(allowed_origins=('http://other.test',)),
            source_id='src', base_url='http://example.test/wfs',
            fetcher=fetcher)
        a = record('a')
        add_ref(a, 'http://other.test/obj#x')
        resolver.register_local(a)
        resolver.resolve_all([a])
        self.assertEqual(a.outbound_refs[0]['state'], 'unparsed')
        self.assertIn('失败', a.outbound_refs[0]['note'])

    def test_relative_url_joins_base_then_whitelist_check(self):
        other = record('y', source='src')

        def fetcher(url):
            self.assertEqual(url, 'http://example.test/obj')
            return 'src', {'y': other}

        resolver = ReferenceResolver(
            ReferencePolicy(allowed_origins=('http://example.test',)),
            source_id='src', base_url='http://example.test/wfs',
            fetcher=fetcher)
        a = record('a')
        add_ref(a, '../obj#y')
        resolver.register_local(a)
        resolver.resolve_all([a])
        self.assertEqual(a.outbound_refs[0]['state'], 'resolved_remote')
        self.assertEqual(a.outbound_refs[0]['note'], 'same_source')


if __name__ == '__main__':
    unittest.main()
