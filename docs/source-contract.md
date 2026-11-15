# 来源和当前接口

## 原始响应

fixtures/sample.json含source_id、url、status、headers、body_base64和可选metadata，headers为有序二元列表，保留重复字段；body_base64解码成真实原始实体字节，原件也存在fixtures/source-body.bin。
app.source.from_packet校验状态、头与字节编码，生成只读Response。header(name)不区分字段名大小写并按顺序逗号连接重复值，headers原元组始终保留。
sha256只证明当前实体字节，不证明来源业务身份或采集完整。fetch执行一次HTTP请求，保留错误响应，不自动跳转，不替来源解释缓存、分页或业务错误。
fetch(url,source_id,method,headers,body,max_bytes)保留方法和请求体，读取超过预算即失败；适配器现有超时为5秒。app.service.collect_one调用该来源接口，inspect(packet)供已有响应读取，二者输出source_id、response_sha256和summary。
实际访问资格由上层来源配置控制，本地来源服务只绑定127.0.0.1；其/source路径返回指定响应，其他路径404。服务器记录请求方法、路径、头与body_base64；现有记录只用于合成来源联调，不包含真实凭据。

## 业务来源口径

使用WFS2.0/GML3.2，geometry支持Point、LineString、Polygon、MultiSurface；srsName可继承，EPSG:4326正式轴序纬/经，CRS84经/纬，输出统一经/纬并保留原序。仅转换明确支持的CRS，其他保留未转换状态。gml:id响应内唯一，业务要素键由typeName+来源稳定id定义。xlink白名单与最大引用深度由配置给出，禁止外部实体和DTD。稳定快照能力由来源显式声明；numberMatched可能unknown，不能只用它证明完整。

## 当前能力与配置

app.baseline.analyze只产生一份响应的概览，兼容入口继续保留；字节摘要不等同于协议或业务完整性。
metadata={}。读取预算、授权范围和来源凭据属于调用配置，真实密钥不放在资料目录或交付证据中。

## WFS2.0采集链路（新增）

新增模块仅用标准库，入口为app.collect与app.harvest.harvest，旧的app.baseline/app.service单响应入口保持不变。

- app.source在原fetch之外增加open_stream/StreamedResponse与stream_from_bytes：实体按需读取，
  字节预算超限即失败，读取过程同步累计sha256，关闭后才给出完整摘要；不自动跳转、不连代理的口径不变。
- app.crs登记来源契约明确授权的CRS（EPSG:4326、EPSG:4979、OGC:CRS84、OGC:CRS84h）。
  srsName接受EPSG:码、URN、HTTP URI等写法并归一化；EPSG:4326轴序纬/经，CRS84经/纬，
  数字相同也按各自契约解释，输出统一经/纬（三维为经/纬/高），原坐标序列始终保留。
  未登记CRS、维数不符、轴范围可疑都生成issue并保留未转换状态，不静默改写、不删除要素。
- app.gmlstream用xml.parsers.expat驱动TreeBuilder做增量解析，整体拒绝DOCTYPE与外部实体
  （含参数实体、外部DTD，内部实体攻击随DOCTYPE一并拒绝）。wfs:member在结束事件即从根摘除，
  大响应不需要整份DOM常驻。支持Point、LineString、Polygon（含内环）、MultiPoint、
  MultiLineString、MultiSurface/Surface与MultiGeometry；核对posList维数、环点数与闭合、
  srsName沿祖先继承。每个FeatureRecord带source_id、page_index、member_index、
  response_sha256与raw_snippet，字段或几何无法解释时issue定位到gml:id与元素路径。
- app.refs处理xlink:href：本响应片段引用统一在一批要素登记后解析（前向引用可命中）；
  远程引用必须命中ReferencePolicy.allowed_origins且不超过max_depth，file:/data:等方案拒绝，
  环、未解析、禁止、超深、跨来源都保留显式state与note，同一规范化URL只抓取一次，绝不递归遍历任意地址。
- app.wfs封装GetCapabilities、DescribeFeatureType、GetFeature（GET KVP与POST XML）。
  先按Capabilities与DescribeFeatureType确认可取类型与几何属性；HTTP 200响应体内的
  ows ExceptionReport按来源异常抛出WfsException，不记成空要素集。分页优先沿用wfs:next，
  并校验next没有改写filter、sortBy、typeNames；无next时仅在来源声明分页且调用方确认稳定排序后
  才以startIndex推进，过滤与排序参数全程不变。
- app.harvest输出HarvestReport：默认observation_scope=bounded_observation（有界观察）。
  只有来源显式具备稳定排序、调用方授权快照口径且无冲突时才给snapshot_complete；
  numberMatched=unknown、跨页numberMatched变化、numberReturned与成员数不符等进入conflicts。
  每页独立记录URL、方法、状态、response_sha256、字节数与issue计数，max_pages限制观察边界。

## 采集配置与联调

HarvestConfig字段：endpoint、source_id、type_name、count、max_pages、filter_xml、sort_by、
method、assume_stable_snapshot、reference_policy（allowed_origins/max_depth）、extra_params、
headers、remote_fetcher。assume_stable_snapshot默认False，稳定性必须由调用方核对契约后显式开启。
app.lab.ScriptedServer在127.0.0.1按路由函数返回能力文档、分页响应或ExceptionReport并记录请求，
仅供合成来源联调；tests全部基于回环服务，不访问真实网络。

