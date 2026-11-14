"""当前单响应概览。"""
import xml.etree.ElementTree as ET
def analyze(response):
    root=ET.fromstring(response.body)
    if root.tag!='{http://www.opengis.net/wfs/2.0}FeatureCollection':raise ValueError('不是要素集合')
    members=root.findall('{http://www.opengis.net/wfs/2.0}member')
    return {'member_count':len(members),'number_matched':root.get('numberMatched')}
