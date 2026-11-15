"""核对CRS标识归一化、轴序契约、维数与单位解释。"""
import unittest

from app import crs


class CrsNormalizationTests(unittest.TestCase):
    def test_srs_name_variants(self):
        cases = {
            'EPSG:4326': 'EPSG:4326',
            'urn:ogc:def:crs:EPSG::4326': 'EPSG:4326',
            'http://www.opengis.net/def/crs/EPSG/0/4326': 'EPSG:4326',
            'urn:ogc:def:crs:OGC:1.3:CRS84': 'OGC:CRS84',
            'OGC:CRS84': 'OGC:CRS84',
            'urn:ogc:def:crs:EPSG::4979': 'EPSG:4979',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(crs.normalize_srs_name(raw), expected)

    def test_unknown_srs(self):
        self.assertIsNone(crs.normalize_srs_name('not-a-crs'))
        key, info = crs.lookup('EPSG:99999')
        self.assertEqual(key, 'EPSG:99999')
        self.assertIsNone(info)


class AxisOrderTests(unittest.TestCase):
    def test_epsg4326_is_lat_lon(self):
        # 30 114 在EPSG:4326契约下解释为 纬30,经114 -> 统一输出经114,纬30
        result = crs.reorder_coordinates([30.0, 114.0],
                                         'urn:ogc:def:crs:EPSG::4326')
        self.assertEqual(result['status'], 'converted')
        self.assertEqual(result['canonical'], [114.0, 30.0])
        self.assertEqual(result['units'], ['degree', 'degree'])

    def test_crs84_is_lon_lat(self):
        # 同样两个数字，CRS84契约下本就是经/纬，绝不能按EPSG:4326翻转
        result = crs.reorder_coordinates([30.0, 114.0],
                                         'urn:ogc:def:crs:OGC:1.3:CRS84')
        self.assertEqual(result['status'], 'converted')
        self.assertEqual(result['canonical'], [30.0, 114.0])

    def test_equal_numbers_different_order_contract(self):
        pair = [30.0, 114.0]
        epsg = crs.reorder_coordinates(pair, 'EPSG:4326')
        crs84 = crs.reorder_coordinates(pair, 'OGC:CRS84')
        self.assertNotEqual(epsg['canonical'], crs84['canonical'])
        # 原序列始终保留
        self.assertEqual(epsg['original'], pair)
        self.assertEqual(crs84['original'], pair)

    def test_three_dimension_axes(self):
        result = crs.reorder_coordinates([30.0, 114.0, 12.5], 'EPSG:4979')
        self.assertEqual(result['status'], 'converted')
        self.assertEqual(result['canonical'], [114.0, 30.0, 12.5])
        self.assertEqual(result['units'][2], 'metre')
        result84 = crs.reorder_coordinates([114.0, 30.0, 12.5], 'OGC:CRS84h')
        self.assertEqual(result84['canonical'], [114.0, 30.0, 12.5])

    def test_dimension_mismatch_is_kept_not_dropped(self):
        result = crs.reorder_coordinates([30.0], 'EPSG:4326')
        self.assertEqual(result['status'], 'invalid')
        self.assertIsNone(result['canonical'])
        self.assertEqual(result['original'], [30.0])
        self.assertTrue(any(i.code == 'dimension_mismatch' for i in result['issues']))

    def test_unsupported_crs_preserved(self):
        result = crs.reorder_coordinates([1.0, 2.0], 'EPSG:32633')
        self.assertEqual(result['status'], 'unsupported_crs')
        self.assertIsNone(result['canonical'])
        self.assertEqual(result['original'], [1.0, 2.0])
        self.assertTrue(any(i.code == 'unsupported_crs' for i in result['issues']))

    def test_suspect_axis_range_flags_conflict(self):
        # 声明纬/经但数值超范围：114不可能是纬度 -> 来源轴序声明可疑
        result = crs.reorder_coordinates([114.0, 30.0], 'EPSG:4326')
        self.assertEqual(result['canonical'], [30.0, 114.0])
        codes = {i.code for i in result['issues']}
        self.assertIn('axis_range_suspect', codes)

    def test_token_parsing(self):
        values, issues = crs.parse_number_tokens('30 114\n 31 115', expected_dimension=2)
        self.assertEqual(values, [30.0, 114.0, 31.0, 115.0])
        self.assertEqual(issues, [])
        values, issues = crs.parse_number_tokens('30 114 31', expected_dimension=2)
        self.assertTrue(any(i.code == 'dimension_mismatch' for i in issues))
        values, issues = crs.parse_number_tokens('30 nope')
        self.assertTrue(any(i.code == 'number_parse' for i in issues))


if __name__ == '__main__':
    unittest.main()
