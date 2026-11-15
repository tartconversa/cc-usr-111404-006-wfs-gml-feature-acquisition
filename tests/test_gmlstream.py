"""核对流式GML解析：几何、多部件、环结构、srsName继承、问题溯源与DTD拒绝。"""
import unittest

from app import gmlstream
from app.source import stream_from_bytes

WFS = 'http://www.opengis.net/wfs/2.0'
GML = 'http://www.opengis.net/gml/3.2'
T = 'urn:transit'


def collection(inner, **attrs):
    attr_text = ''.join(' %s="%s"' % item for item in attrs.items())
    return ('<wfs:FeatureCollection xmlns:wfs="%s" xmlns:gml="%s" xmlns:t="%s"'
            ' xmlns:xlink="http://www.w3.org/1999/xlink"%s>'
            '<wfs:member>%s</wfs:member></wfs:FeatureCollection>'
            % (WFS, GML, T, attr_text, inner)).encode('utf-8')


def parse_one(body, page_index=0):
    streamed = stream_from_bytes('src', 'http://example.test/wfs', 200,
                                 [['Content-Type', 'application/gml+xml']], body)
    holder = {}
    header, members = gmlstream.iter_feature_collection(
        streamed.stream, source_id='src',
        response_sha256_getter=lambda: 'sha-test', root_holder=holder)
    records = list(members)
    streamed.close()
    return header, records, holder, streamed


class PointAxisTests(unittest.TestCase):
    def test_epsg4326_point_flips_to_lon_lat(self):
        body = collection(
            '<t:Stop gml:id="s1"><t:point>'
            '<gml:Point srsName="urn:ogc:def:crs:EPSG::4326">'
            '<gml:pos>30 114</gml:pos></gml:Point></t:point></t:Stop>',
            numberMatched='1', numberReturned='1')
        header, records, _, _ = parse_one(body)
        self.assertEqual(header.number_matched, '1')
        record = records[0]
        self.assertEqual(record.gml_id, 's1')
        self.assertEqual(record.type_name, '{%s}Stop' % T)
        geom = record.geometries[0]
        self.assertEqual(geom['type'], 'Point')
        self.assertEqual(geom['coordinates'], [114.0, 30.0])
        self.assertEqual(geom['crs_key'], 'EPSG:4326')
        self.assertEqual(record.response_sha256, 'sha-test')

    def test_crs84_point_keeps_order(self):
        body = collection(
            '<t:Stop gml:id="s2"><t:point>'
            '<gml:Point srsName="urn:ogc:def:crs:OGC:1.3:CRS84">'
            '<gml:pos>30 114</gml:pos></gml:Point></t:point></t:Stop>')
        _, records, _, _ = parse_one(body)
        self.assertEqual(records[0].geometries[0]['coordinates'], [30.0, 114.0])

    def test_inherited_srs_name_from_collection(self):
        body = collection(
            '<t:Stop gml:id="s3"><t:point><gml:Point><gml:pos>30 114</gml:pos>'
            '</gml:Point></t:point></t:Stop>',
            **{'srsName': 'urn:ogc:def:crs:EPSG::4326'})
        _, records, _, _ = parse_one(body)
        geom = records[0].geometries[0]
        self.assertEqual(geom['coordinates'], [114.0, 30.0])
        self.assertEqual(geom['srs_name'], 'urn:ogc:def:crs:EPSG::4326')

    def test_missing_srs_is_issue_not_silent_drop(self):
        body = collection(
            '<t:Stop gml:id="s4"><t:point><gml:Point><gml:pos>30 114</gml:pos>'
            '</gml:Point></t:point></t:Stop>')
        _, records, _, _ = parse_one(body)
        record = records[0]
        self.assertEqual(len(record.geometries), 1)
        self.assertTrue(any(i.code == 'missing_srs' for i in record.issues))
        # 原响应片段可回溯
        self.assertIn('s4', record.raw_snippet)


class GeometryStructureTests(unittest.TestCase):
    def test_linestring_poslist_dimension(self):
        body = collection(
            '<t:Path gml:id="l1"><t:geom><gml:LineString '
            'srsName="EPSG:4326" srsDimension="2">'
            '<gml:posList>30 114 31 115</gml:posList>'
            '</gml:LineString></t:geom></t:Path>')
        _, records, _, _ = parse_one(body)
        coords = records[0].geometries[0]['coordinates']
        self.assertEqual(coords, [[114.0, 30.0], [115.0, 31.0]])

    def test_polygon_rings_open_ring_reported(self):
        body = collection(
            '<t:Zone gml:id="p1"><t:area><gml:Polygon srsName="OGC:CRS84">'
            '<gml:exterior><gml:LinearRing><gml:posList>'
            '114 30 115 30 115 31 114 30</gml:posList></gml:LinearRing>'
            '</gml:exterior>'
            '<gml:interior><gml:LinearRing><gml:posList>'
            '114.2 30.2 114.8 30.2 114.8 30.8</gml:posList></gml:LinearRing>'
            '</gml:interior></gml:Polygon></t:area></t:Zone>')
        _, records, _, _ = parse_one(body)
        polygon = records[0].geometries[0]
        self.assertEqual(polygon['type'], 'Polygon')
        exterior, interior = polygon['coordinates']
        self.assertEqual(exterior[0], [114.0, 30.0])
        self.assertEqual(exterior[-1], exterior[0])  # 闭合环
        self.assertEqual(len(interior), 3)
        codes = {i.code for i in records[0].issues}
        self.assertIn('ring_not_closed', codes)
        self.assertIn('ring_too_few_points', codes)

    def test_multisurface_multi_parts(self):
        body = collection(
            '<t:Hub gml:id="m1"><t:area>'
            '<gml:MultiSurface srsName="OGC:CRS84">'
            '<gml:surfaceMember><gml:Polygon><gml:exterior>'
            '<gml:LinearRing><gml:posList>0 0 1 0 1 1 0 0</gml:posList>'
            '</gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>'
            '<gml:surfaceMember><gml:Polygon><gml:exterior>'
            '<gml:LinearRing><gml:posList>2 2 3 2 3 3 2 2</gml:posList>'
            '</gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>'
            '</gml:MultiSurface></t:area></t:Hub>')
        _, records, _, _ = parse_one(body)
        geom = records[0].geometries[0]
        self.assertEqual(geom['type'], 'MultiPolygon')
        self.assertEqual(len(geom['coordinates']), 2)
        # 同质多部件展开为坐标嵌套，与Polygon结构一致
        first_polygon = geom['coordinates'][0]
        self.assertEqual(first_polygon[0][0], [0.0, 0.0])

    def test_bad_number_is_attributed_to_feature(self):
        body = collection(
            '<t:Stop gml:id="bad1"><t:point>'
            '<gml:Point srsName="EPSG:4326"><gml:pos>oops 114</gml:pos>'
            '</gml:Point></t:point></t:Stop>')
        _, records, _, _ = parse_one(body)
        record = records[0]
        self.assertTrue(any(i.code == 'number_parse' for i in record.issues))
        self.assertEqual(record.issues[0].gml_id, 'bad1')
        self.assertTrue(record.issues[0].path)


class StreamSafetyTests(unittest.TestCase):
    def test_doctype_rejected(self):
        body = ('<?xml version="1.0"?><!DOCTYPE wfs:FeatureCollection ['
                '<!ENTITY x "bomb">]>'
                '<wfs:FeatureCollection xmlns:wfs="%s"/>' % WFS).encode()
        with self.assertRaises(gmlstream.DtdForbidden):
            parse_one(body)

    def test_external_entity_rejected(self):
        body = ('<?xml version="1.0"?><!DOCTYPE wfs:FeatureCollection ['
                '<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                '<wfs:FeatureCollection xmlns:wfs="%s"/>' % WFS).encode()
        with self.assertRaises(gmlstream.DtdForbidden):
            parse_one(body)

    def test_wrong_root(self):
        with self.assertRaises(gmlstream.GmlFormatError):
            parse_one(b'<something xmlns="x"/>')

    def test_duplicate_gml_id_conflict(self):
        body = ('<wfs:FeatureCollection xmlns:wfs="%s" xmlns:gml="%s" xmlns:t="%s">'
                '<wfs:member><t:Stop gml:id="dup"><t:name>a</t:name></t:Stop>'
                '</wfs:member><wfs:member><t:Stop gml:id="dup"><t:name>b</t:name>'
                '</t:Stop></wfs:member></wfs:FeatureCollection>'
                % (WFS, GML, T)).encode()
        _, records, _, _ = parse_one(body)
        self.assertEqual(len(records), 2)
        self.assertTrue(any(i.code == 'duplicate_gml_id' for i in records[1].issues))

    def test_many_members_do_not_keep_full_dom(self):
        parts = ['<wfs:FeatureCollection xmlns:wfs="%s" xmlns:gml="%s" xmlns:t="%s" '
                 'numberMatched="500" numberReturned="500">' % (WFS, GML, T)]
        for index in range(500):
            parts.append(
                '<wfs:member><t:Stop gml:id="s%d"><t:point>'
                '<gml:Point srsName="OGC:CRS84"><gml:pos>%d 30</gml:pos></gml:Point>'
                '</t:point></t:Stop></wfs:member>' % (index, index % 180))
        parts.append('</wfs:FeatureCollection>')
        body = ''.join(parts).encode()
        streamed = stream_from_bytes('src', 'u', 200, [], body)
        holder = {}
        header, members = gmlstream.iter_feature_collection(
            streamed.stream, source_id='src',
            response_sha256_getter=lambda: None, root_holder=holder)
        count = 0
        for record in members:
            count += 1
            # 根下被清理过，任意时刻直接成员数应保持很小
            self.assertLessEqual(len(holder['root']), 2)
        self.assertEqual(count, 500)
        streamed.close()


class PropertyAndRefTests(unittest.TestCase):
    def test_xlink_ref_is_state_not_fetched_here(self):
        body = collection(
            '<t:Stop gml:id="r1"><t:station xlink:href="#s99"/>'
            '<t:external xlink:href="http://other.test/obj#x"/></t:Stop>')
        _, records, _, _ = parse_one(body)
        refs = records[0].outbound_refs
        states = {r['href']: r['state'] for r in refs}
        self.assertEqual(states['#s99'], 'pending_local')
        self.assertEqual(states['http://other.test/obj#x'], 'pending_remote')

    def test_nil_property(self):
        body = collection(
            '<t:Stop gml:id="n1" gml:nil="true"><t:name/></t:Stop>')
        _, records, _, _ = parse_one(body)
        self.assertIsNone(records[0].properties.get('{%s}name' % T))


class ExceptionReportSniffTests(unittest.TestCase):
    def test_exception_report_parsed(self):
        body = ('<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1" '
                'version="2.0.0"><ows:Exception exceptionCode="InvalidParameterValue" '
                'locator="typeNames"><ows:ExceptionText>bad type</ows:ExceptionText>'
                '</ows:Exception></ows:ExceptionReport>').encode()
        report = gmlstream.parse_exception_report(body)
        self.assertEqual(report['exceptions'][0]['exception_code'],
                         'InvalidParameterValue')
        self.assertEqual(report['exceptions'][0]['texts'], ['bad type'])

    def test_normal_collection_is_not_exception(self):
        body = collection('<t:Stop gml:id="x"/>')
        self.assertIsNone(gmlstream.parse_exception_report(body))


if __name__ == '__main__':
    unittest.main()
