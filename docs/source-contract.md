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

当前app.baseline.analyze只产生一份响应的概览，兼容入口继续保留；新采集可增加入口，不能把字节摘要等同于协议或业务完整性。
metadata={}。读取预算、授权范围和来源凭据属于调用配置，真实密钥不放在资料目录或交付证据中。
