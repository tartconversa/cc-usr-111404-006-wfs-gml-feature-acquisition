"""WFS2.0采集命令行入口：逐页流式采集，要素按JSONL交付。

用法：
  python -m app.collect <配置JSON路径> [--out 要素.jsonl] [--report 报告.json]

配置JSON字段对应HarvestConfig；reference_policy用allowed_origins/max_depth表达。
不配置输出文件时要素写到标准输出，每行一个要素记录，报告写到标准错误。
"""
import argparse
import json
import sys
from dataclasses import asdict, fields
from pathlib import Path

from .gmlstream import Issue
from .harvest import HarvestConfig, harvest
from .refs import ReferencePolicy


def _issue_to_dict(issue):
    if isinstance(issue, Issue):
        return {'code': issue.code, 'message': issue.message,
                'path': issue.path, 'gml_id': issue.gml_id,
                'snippet': issue.snippet}
    return {'code': getattr(issue, 'code', 'issue'),
            'message': str(issue), 'path': getattr(issue, 'path', '')}


def _record_to_dict(record):
    return {
        'feature_key': record.feature_key,
        'type_name': record.type_name,
        'gml_id': record.gml_id,
        'properties': record.properties,
        'geometries': record.geometries,
        'issues': [_issue_to_dict(item) for item in record.issues],
        'outbound_refs': record.outbound_refs,
        'source_id': record.source_id,
        'response_sha256': record.response_sha256,
        'page_index': record.page_index,
        'member_index': record.member_index,
        'raw_snippet': record.raw_snippet,
    }


def config_from_dict(data):
    policy_data = data.pop('reference_policy', {}) or {}
    policy = ReferencePolicy(
        allowed_origins=tuple(policy_data.get('allowed_origins', ())),
        max_depth=int(policy_data.get('max_depth', 3)))
    allowed = {item.name for item in fields(HarvestConfig)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError('配置含未知字段: %s' % sorted(unknown))
    return HarvestConfig(reference_policy=policy, **data)


def run(config, *, out_stream=sys.stdout, report_stream=sys.stderr):
    records, report = harvest(config)
    for record in records:
        out_stream.write(json.dumps(_record_to_dict(record),
                                    ensure_ascii=False, default=str) + '\n')
    report_stream.write(json.dumps(asdict(report), ensure_ascii=False,
                                   indent=2, default=str) + '\n')
    return records, report


def main(argv=None):
    parser = argparse.ArgumentParser(description='WFS2.0/GML3.2受控采集')
    parser.add_argument('config', help='采集配置JSON路径')
    parser.add_argument('--out', help='要素JSONL输出路径（默认标准输出）')
    parser.add_argument('--report', help='采集报告JSON输出路径（默认标准错误）')
    args = parser.parse_args(argv)
    data = json.loads(Path(args.config).read_text(encoding='utf-8'))
    config = config_from_dict(dict(data))
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as out_stream:
            if args.report:
                with open(args.report, 'w', encoding='utf-8') as report_stream:
                    run(config, out_stream=out_stream, report_stream=report_stream)
            else:
                run(config, out_stream=out_stream)
    else:
        run(config)


if __name__ == '__main__':
    main()
