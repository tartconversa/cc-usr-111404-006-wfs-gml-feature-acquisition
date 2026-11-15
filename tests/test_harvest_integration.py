"""核对流式预算、startIndex分页、跨页冲突、引用端到端接入与溯源回填。"""
import json
import unittest

from app.lab import ScriptedServer
from app.source import open_stream, stream_from_bytes
from app.harvest import HarvestConfig, harvest
from app.refs import ReferencePolicy
from app.gmlstream import iter_feature_collection

from tests.test_wfs import CAPABILITIES, SCHEMA, feature_page, packet


class StreamBudgetTests(unittest.TestCase):
    def test_open_stream_enforces_budget(self):
        def router(method, path, query, body):
            return packet(b'<x>' + b'a' * 100 + b'</x>')

        with ScriptedServer(router) as server:
            streamed = open_stream(server.url, 'src', max_bytes=50)
            with self.assertRaisesRegex(ValueError, '预算'):
                streamed.stream.read()
            streamed.close()

    def test_memory_stream_budget_parity(self):
        with self.assertRaisesRegex(ValueError, '预算'):
            stream_from_bytes('src', 'u', 200, [], b'abcdef', max_bytes=2)

    def test_sha_only_available_after_close(self):
        streamed = stream_from_bytes('src', 'u', 200, [], b'abc')
        with self.assertRaises(RuntimeError):
            _ = streamed.sha256
        streamed.stream.read()
        streamed.close()
        self.assertEqual(len(streamed.sha256), 64)


class StartIndexPagingTests(unittest.TestCase):
    def test_offset_paging_requires_stable_order_and_keeps_query(self):
        requests_seen = []

        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)  # 声明支持ResultSetPaging
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            if query.get('request') == 'GetFeature':
                requests_seen.append(query)
                start = int(query.get('startIndex', '0'))
                ids = ['s%d' % i for i in range(start, min(start + 2, 5))]
                # 不返回next链接，迫使客户端走startIndex
                return packet(feature_page(ids, start, 5, None))
            return None

        with ScriptedServer(router) as server:
            # 未授权稳定排序：只能观察第一页
            config = HarvestConfig(endpoint=server.url, source_id='src',
                                   type_name='t:Stop', count=2, max_pages=5,
                                   sort_by='t:name')
            records, report = harvest(config)
            self.assertEqual(len(records), 2)
            self.assertEqual(report.stopped_reason, 'no_stable_order_for_offset')
            self.assertEqual(report.observation_scope, 'bounded_observation')

            # 显式授权后：startIndex按页推进且过滤/排序保持
            requests_seen.clear()
            config2 = HarvestConfig(endpoint=server.url, source_id='src',
                                    type_name='t:Stop', count=2, max_pages=5,
                                    sort_by='t:name',
                                    filter_xml='<fes:Filter/>',
                                    assume_stable_snapshot=True)
            records2, report2 = harvest(config2)
            self.assertEqual(len(records2), 5)
            offsets = [int(q['startIndex']) for q in requests_seen]
            self.assertEqual(offsets, [0, 2, 4])
            for query in requests_seen:
                self.assertEqual(query['sortBy'], 't:name')
                self.assertEqual(query['filter'], '<fes:Filter/>')

    def test_cross_page_number_matched_change_is_conflict(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            start = int(query.get('startIndex', '0'))
            total = 5 if start == 0 else 7  # 来源在第二页改了numberMatched
            ids = ['s%d' % i for i in range(start, min(start + 2, total))]
            from urllib.parse import urlencode
            nxt = None
            if start == 0:
                qs = urlencode([('request', 'GetFeature'), ('typeNames', 't:Stop'),
                                ('startIndex', '2')]).replace('&', '&amp;')
                nxt = server.url + '/wfs?' + qs
            xml = feature_page(ids, start, total, nxt)
            return packet(xml)

        with ScriptedServer(router) as server:
            config = HarvestConfig(endpoint=server.url, source_id='src',
                                   type_name='t:Stop', count=2, max_pages=5)
            records, report = harvest(config)
            self.assertTrue(any('numberMatched' in c for c in report.conflicts))
            self.assertEqual(report.observation_scope, 'bounded_observation')


class EndToEndReferenceTests(unittest.TestCase):
    def test_remote_ref_resolved_through_harvest_fetcher(self):
        # 主来源要素引用授权白名单内另一来源的对象
        main_xml = feature_page(['s0'], 0, 1).replace(
            '</wfs:FeatureCollection>',
            '<wfs:member><t:Stop gml:id="s1"><t:station '
            'xmlns:xlink="http://www.w3.org/1999/xlink" '
            'xlink:href="http://auth.test/obj#r9"/></t:Stop>'
            '</wfs:member></wfs:FeatureCollection>')

        remote_xml = feature_page([], 0, 0).replace(
            '</wfs:FeatureCollection>',
            '<wfs:member><t:Stop gml:id="r9"><t:name>Remote</t:name>'
            '</t:Stop></wfs:member></wfs:FeatureCollection>')

        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            return packet(main_xml)

        def remote_fetcher(url):
            self.assertEqual(url, 'http://auth.test/obj')
            streamed = stream_from_bytes('auth', url, 200, [],
                                         remote_xml.encode('utf-8'))
            holder = {}
            _, members = iter_feature_collection(
                streamed.stream, source_id='auth',
                response_sha256_getter=lambda: None, root_holder=holder)
            index = {record.gml_id: record for record in members}
            streamed.close()
            return 'auth', index

        with ScriptedServer(router) as server:
            config = HarvestConfig(
                endpoint=server.url, source_id='src', type_name='t:Stop',
                count=10,
                reference_policy=ReferencePolicy(
                    allowed_origins=('http://auth.test',), max_depth=2),
                remote_fetcher=remote_fetcher)
            records, report = harvest(config)
            ref_record = next(r for r in records if r.gml_id == 's1')
            state = ref_record.outbound_refs[0]
            self.assertEqual(state['state'], 'resolved_remote')
            self.assertEqual(state['resolved_from'], 'auth')
            self.assertEqual(report.unresolved_refs, 0)

    def test_forbidden_remote_ref_counted_unresolved(self):
        main_xml = feature_page([], 0, 0).replace(
            '</wfs:FeatureCollection>',
            '<wfs:member><t:Stop gml:id="s1"><t:station '
            'xmlns:xlink="http://www.w3.org/1999/xlink" '
            'xlink:href="http://evil.test/obj#r"/></t:Stop>'
            '</wfs:member></wfs:FeatureCollection>')

        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            return packet(main_xml)

        with ScriptedServer(router) as server:
            config = HarvestConfig(endpoint=server.url, source_id='src',
                                   type_name='t:Stop', count=10)
            records, report = harvest(config)
            state = records[0].outbound_refs[0]
            self.assertEqual(state['state'], 'forbidden')
            self.assertEqual(report.unresolved_refs, 1)


class CliSmokeTests(unittest.TestCase):
    def test_collect_cli_streams_jsonl(self):
        import io
        from app import collect

        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            return packet(feature_page(['s0'], 0, 1))

        with ScriptedServer(router) as server:
            config = HarvestConfig(endpoint=server.url, source_id='src',
                                   type_name='t:Stop', count=10)
            out, report_io = io.StringIO(), io.StringIO()
            records, report = collect.run(config, out_stream=out,
                                          report_stream=report_io)
        lines = [line for line in out.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload['gml_id'], 's0')
        self.assertIn('observation_scope', report_io.getvalue())
        self.assertEqual(report.total_members, 1)


class ProvenanceTests(unittest.TestCase):
    def test_feature_carries_page_and_response_identity(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            return packet(feature_page(['s0'], 0, 1))

        with ScriptedServer(router) as server:
            config = HarvestConfig(endpoint=server.url, source_id='src',
                                   type_name='t:Stop', count=10)
            records, report = harvest(config)
            record = records[0]
            self.assertEqual(record.source_id, 'src')
            self.assertEqual(record.page_index, 0)
            self.assertEqual(record.response_sha256,
                             report.pages[0]['response_sha256'])
            self.assertIn('s0', record.raw_snippet)
            self.assertTrue(record.feature_key.startswith('src:'))


if __name__ == '__main__':
    unittest.main()
