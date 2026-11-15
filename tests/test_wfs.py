"""核对WFS2.0能力确认、类型描述、分页口径、异常识别与有界观察报告。"""
import base64
import unittest

from app.lab import ScriptedServer
from app.wfs import WfsClient, WfsException
from app.harvest import HarvestConfig, harvest
from app.refs import ReferencePolicy

WFS = 'http://www.opengis.net/wfs/2.0'
OWS = 'http://www.opengis.net/ows/1.1'
GML = 'http://www.opengis.net/gml/3.2'
T = 'urn:transit'


def packet(body, status=200, content_type='application/xml'):
    if isinstance(body, str):
        body = body.encode('utf-8')
    return {'status': status,
            'headers': [['Content-Type', content_type]],
            'body_base64': base64.b64encode(body).decode()}


CAPABILITIES = '''<wfs:WFS_Capabilities xmlns:wfs="%(wfs)s" xmlns:ows="%(ows)s"
 version="2.0.0">
 <ows:OperationsMetadata>
  <ows:Constraint name="ImplementsResultSetPaging"><ows:AllowedValues>
   <ows:Value>TRUE</ows:Value><ows:Value>FALSE</ows:Value></ows:AllowedValues>
   <ows:DefaultValue>TRUE</ows:DefaultValue></ows:Constraint>
  <ows:Constraint name="CountDefault"><ows:DefaultValue>200</ows:DefaultValue></ows:Constraint>
  <ows:Constraint name="GetFeatureOutputFormats"><ows:AllowedValues>
   <ows:Value>application/gml+xml; version=3.2</ows:Value></ows:AllowedValues>
   <ows:DefaultValue>application/gml+xml; version=3.2</ows:DefaultValue>
  </ows:Constraint>
 </ows:OperationsMetadata>
 <wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>t:Stop</wfs:Name></wfs:FeatureType>
  <wfs:FeatureType><wfs:Name>t:Hub</wfs:Name></wfs:FeatureType></wfs:FeatureTypeList>
</wfs:WFS_Capabilities>''' % {'wfs': WFS, 'ows': OWS}

SCHEMA = '''<schema xmlns="http://www.w3.org/2001/XMLSchema" xmlns:gml="%(gml)s"
 xmlns:t="%(t)s" targetNamespace="%(t)s" elementFormDefault="qualified">
 <element name="Stop" type="t:StopType"/>
 <complexType name="StopType">
  <complexContent><extension base="gml:AbstractFeatureType">
   <sequence>
    <element name="name" type="string"/>
    <element name="point" type="gml:PointType"/>
   </sequence>
  </extension></complexContent>
 </complexType>
</schema>''' % {'gml': GML, 't': T}

EXCEPTION = '''<ows:ExceptionReport xmlns:ows="%(ows)s" version="2.0.0">
 <ows:Exception exceptionCode="InvalidParameterValue" locator="startIndex">
  <ows:ExceptionText>startIndex超出结果集</ows:ExceptionText>
 </ows:Exception></ows:ExceptionReport>''' % {'ows': OWS}


def feature_page(ids, start, total, next_url=None):
    members = []
    for fid in ids:
        members.append(
            '<wfs:member><t:Stop gml:id="%(id)s"><t:name>Stop %(id)s</t:name>'
            '<t:point><gml:Point srsName="urn:ogc:def:crs:EPSG::4326">'
            '<gml:pos>30 114</gml:pos></gml:Point></t:point></t:Stop></wfs:member>'
            % {'id': fid})
    next_link = ('<wfs:next xlink:href="%s" xmlns:xlink="http://www.w3.org/1999/xlink"/>'
                 % next_url) if next_url else ''
    return ('<wfs:FeatureCollection xmlns:wfs="%(wfs)s" xmlns:gml="%(gml)s" '
            'xmlns:t="%(t)s" numberMatched="%(total)d" numberReturned="%(n)d" '
            'timeStamp="2026-11-15T00:00:00Z">%(members)s%(next)s'
            '</wfs:FeatureCollection>'
            % {'wfs': WFS, 'gml': GML, 't': T, 'total': total,
               'n': len(ids), 'members': ''.join(members), 'next': next_link})


class ProtocolTests(unittest.TestCase):
    def test_capabilities_and_describe(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA, content_type='application/xml')
            return None

        with ScriptedServer(router) as server:
            client = WfsClient(server.url, 'src')
            caps = client.get_capabilities()
            self.assertTrue(caps.supports_paging())
            self.assertEqual(caps.default_count, 200)
            self.assertIn('t:Stop', caps.feature_type_names)
            desc = client.describe_feature_type('t:Stop', capabilities=caps)
            prop_names = [item[0] for item in desc.properties]
            self.assertIn('name', prop_names)
            self.assertIn('point', prop_names)
            self.assertEqual(desc.geometry_properties, ('point',))
            # 请求确实带WFS2.0参数
            request = server.requests[0]
            self.assertEqual(request['query']['version'], '2.0.0')

    def test_exception_report_inside_http_200_is_not_empty_set(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            return packet(EXCEPTION, status=200)  # 故意200

        with ScriptedServer(router) as server:
            client = WfsClient(server.url, 'src')
            page = client.build_page_request('t:Stop', count=10)
            with self.assertRaises(WfsException) as ctx:
                client.open_page(page)
            self.assertEqual(ctx.exception.report['exceptions'][0]['exception_code'],
                             'InvalidParameterValue')
            self.assertEqual(ctx.exception.status, 200)

    def test_get_feature_keeps_filter_and_sorting(self):
        seen = []

        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            seen.append(query)
            start = int(query.get('startIndex', '0'))
            page_ids = ['s%d' % i for i in range(start, min(start + 2, 5))]
            nxt = None
            if start + 2 < 5:
                # next链接必须完整回显过滤与排序口径，否则采集器应拒绝；
                # URL出现在XML属性中，&必须转义为&amp;
                from urllib.parse import urlencode
                query_string = urlencode([
                    ('request', 'GetFeature'), ('typeNames', 't:Stop'),
                    ('filter', query['filter']), ('sortBy', query['sortBy']),
                    ('startIndex', str(start + 2))])
                nxt = server_url + '/wfs?' + query_string.replace('&', '&amp;')
            return packet(feature_page(page_ids, start, 5, nxt))

        with ScriptedServer(router) as server:
            server_url = server.url
            config = HarvestConfig(
                endpoint=server.url, source_id='src', type_name='t:Stop',
                count=2, max_pages=5, sort_by='t:name',
                filter_xml='<fes:Filter/>', assume_stable_snapshot=True,
                reference_policy=ReferencePolicy())
            records, report = harvest(config)
            self.assertEqual(len(records), 5)
            # 每一页请求都保留过滤与排序
            for query in seen:
                if query.get('request') == 'GetFeature':
                    self.assertEqual(query.get('sortBy'), 't:name')
                    self.assertEqual(query.get('filter'), '<fes:Filter/>')
            self.assertEqual(report.observation_scope, 'snapshot_complete')
            self.assertEqual(len(report.pages), 3)
            # EPSG:4326 轴序已转经/纬
            self.assertEqual(records[0].geometries[0]['coordinates'], [114.0, 30.0])

    def test_next_link_dropping_filter_is_rejected(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            if 'startIndex' not in query or query['startIndex'] == '0':
                nxt = (server.url + '/wfs?request=GetFeature&amp;'
                       'typeNames=t:Stop')
                return packet(feature_page(['s0', 's1'], 0, 5, nxt))
            return packet(feature_page(['s2'], 2, 5))

        with ScriptedServer(router) as server:
            config = HarvestConfig(
                endpoint=server.url, source_id='src', type_name='t:Stop',
                count=2, max_pages=5, filter_xml='<fes:Filter/>',
                sort_by='t:name')
            with self.assertRaises(WfsException) as ctx:
                harvest(config)
            self.assertIn('过滤', str(ctx.exception))

    def test_without_stable_order_only_bounded_observation(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            return packet(feature_page(['s0'], 0, 1))  # 无next链接

        with ScriptedServer(router) as server:
            config = HarvestConfig(
                endpoint=server.url, source_id='src', type_name='t:Stop',
                count=2, max_pages=5)  # 未授权稳定快照
            records, report = harvest(config)
            self.assertEqual(len(records), 1)
            self.assertEqual(report.observation_scope, 'bounded_observation')
            self.assertIn('不承诺', report.snapshot_claim)

    def test_unknown_number_matched_is_conflict_not_completeness(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            body_xml = feature_page(['s0'], 0, 1).replace(
                'numberMatched="1"', 'numberMatched="unknown"')
            return packet(body_xml)

        with ScriptedServer(router) as server:
            config = HarvestConfig(endpoint=server.url, source_id='src',
                                   type_name='t:Stop', count=2)
            records, report = harvest(config)
            self.assertTrue(any('unknown' in c for c in report.conflicts))
            self.assertEqual(report.observation_scope, 'bounded_observation')

    def test_undeclared_type_rejected(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            return packet(SCHEMA)

        with ScriptedServer(router) as server:
            config = HarvestConfig(endpoint=server.url, source_id='src',
                                   type_name='t:NotDeclared', count=2)
            with self.assertRaises(WfsException):
                harvest(config)

    def test_each_page_has_sha_and_issues_are_attributed(self):
        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            bad = ('<wfs:member><t:Stop gml:id="bad"><t:point>'
                   '<gml:Point srsName="EPSG:4326"><gml:pos>x y</gml:pos>'
                   '</gml:Point></t:point></t:Stop></wfs:member>')
            xml = feature_page(['ok'], 0, 2).replace('</wfs:FeatureCollection>',
                                                      bad + '</wfs:FeatureCollection>')
            return packet(xml)

        with ScriptedServer(router) as server:
            config = HarvestConfig(endpoint=server.url, source_id='src',
                                   type_name='t:Stop', count=10)
            records, report = harvest(config)
            self.assertEqual(len(records), 2)
            self.assertEqual(len(report.pages[0]['response_sha256']), 64)
            bad_record = next(r for r in records if r.gml_id == 'bad')
            self.assertTrue(bad_record.issues)
            self.assertEqual(bad_record.response_sha256,
                             report.pages[0]['response_sha256'])


class PostProtocolTests(unittest.TestCase):
    def test_post_getfeature_body_contains_filter(self):
        captured = {}

        def router(method, path, query, body):
            if query.get('request') == 'GetCapabilities':
                return packet(CAPABILITIES)
            if query.get('request') == 'DescribeFeatureType':
                return packet(SCHEMA)
            if method == 'POST':
                captured['body'] = body.decode('utf-8')
                return packet(feature_page(['s0'], 0, 1))
            return None

        with ScriptedServer(router) as server:
            config = HarvestConfig(
                endpoint=server.url, source_id='src', type_name='t:Stop',
                method='POST', filter_xml='<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"/>',
                sort_by='t:name')
            records, report = harvest(config)
            self.assertEqual(len(records), 1)
            self.assertIn('GetFeature', captured['body'])
            self.assertIn('fes:Filter', captured['body'])
            self.assertIn('SortBy', captured['body'])


if __name__ == '__main__':
    unittest.main()
