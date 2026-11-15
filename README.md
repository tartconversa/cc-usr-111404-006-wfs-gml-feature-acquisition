# 交通设施WFS来源的坐标轴与要素引用采集

现有组件读取一份来源响应，输出当前概览及字节摘要。尚不具备持续领取或跨响应业务归并能力。
app/source.py保留响应状态、重复头和实体原字节，一次请求不自动跟随重定向；app/baseline.py解释当前单响应，app/service.py将概览绑定到来源身份及摘要。
app/lab.py提供回环HTTP来源，支持GET、POST、HEAD、PROPFIND和REPORT，记录收到的请求，用于在可控来源上联调。fixtures为合成资料，不连接真实企业或个人。
WFS2.0采集链路：app/crs.py解释CRS标识、轴序、维数与单位（EPSG:4326纬/经、CRS84经/纬，不混为同序）；app/gmlstream.py在禁DTD的前提下流式解析GML成员并即时摘除DOM；app/refs.py按白名单、深度与环状态受控解析xlink；app/wfs.py实现Capabilities、DescribeFeatureType、GetFeature与分页，识别HTTP 200内的ExceptionReport；app/harvest.py编排采集并给出有界观察或快照报告；app/collect.py提供JSONL命令行入口。细节见[来源契约](docs/source-contract.md)。
来源协议、字段身份及当前边界见[来源契约](docs/source-contract.md)。Python 3.14，现有代码仅使用标准库（在Python 3.11标准库下同样可运行）。

读取当前响应概览：

    python -m app.service fixtures/sample.json

运行WFS2.0采集（配置JSON字段对应app.harvest.HarvestConfig）：

    python -m app.collect config.json --out records.jsonl --report report.json

运行现有模块检查：

    python -m unittest discover -s tests -v

检查源码编译：

    python -m compileall -q app tests

如需单独启动本地来源，运行下列命令后读取打印的回环地址；结束时按Ctrl+C：

    python -m app.lab fixtures/sample.json
