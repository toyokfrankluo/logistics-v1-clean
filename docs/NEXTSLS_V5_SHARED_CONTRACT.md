# NextSLS V5 共用创建规范与证据（2026-11-18）

> 历史证据记录。后续用户明确授权采用通用HTTPS photos默认编码及旧版未知规则单票尝试；当前运行策略见 [NEXTSLS_V5_COMPATIBILITY.md](NEXTSLS_V5_COMPATIBILITY.md)。以下“待确认阻断”和渠道数量描述属于该次验收，不能作为新版运行策略。

范围：Render 测试站，12 家已有代理；本轮不发送任何 shipment/create。逐家读取实际 `/api/v5/docs/markdown`，比较创建字段表、渠道字段表、图片定义、附件结构及查询返回。下表为当前文档正文 SHA-256，便于复核版本；凭据不在本文或源码中。

| Render 代理 ID | 供应商 | 官方文档 | 正文 SHA-256 |
| --- | --- | --- | --- |
| 2 | 中威供应链管理服务（深圳）有限公司 | [https://zwgylx.nextsls.com/api/v5/docs](https://zwgylx.nextsls.com/api/v5/docs) | `1ba7930b3da30fed04c6c1ccc4e21bcc80733b65fcc607b7cc1fcd715b152435` |
| 4 | 博诚 | [https://bckjkj.nextsls.com/api/v5/docs](https://bckjkj.nextsls.com/api/v5/docs) | `3a8539f03d0aa2002c58828c8fc2cce01284915b3a896c287efca2588e82a3cc` |
| 5 | jsd | [https://jinsda.nextsls.com/api/v5/docs](https://jinsda.nextsls.com/api/v5/docs) | `16dfe1cc5323aceb9b97afd92912132b86415f4faa1cb447791bc395d53808c5` |
| 6 | 亿阳 | [https://yiyang.nextsls.com/api/v5/docs](https://yiyang.nextsls.com/api/v5/docs) | `abdb73c48c4a3c38f9e2b9904461a70f6fa876cb6f668e1c76877197c594ac5f` |
| 7 | 壹合国际 | [https://hbqexp.nextsls.com/api/v5/docs](https://hbqexp.nextsls.com/api/v5/docs) | `89f753770663ee727fc025302526c6c317c028610c14074d7bd3c055a84bbf7f` |
| 8 | 广东蓝图梦跨境供应链有限公司 | [https://gdltkj.nextsls.com/api/v5/docs](https://gdltkj.nextsls.com/api/v5/docs) | `87590e642e20989995b585d72e22322c31c017fc525eb8e917fc384c5744f9c7` |
| 9 | 梁鑫 | [https://szlxgj.nextsls.com/api/v5/docs](https://szlxgj.nextsls.com/api/v5/docs) | `2e7d735b9ad66e91175473e823e838180290b13f5ac690eb1eab9d1056c672ba` |
| 10 | 深圳小满速达物流有限公司 | [https://xmsdwl.nextsls.com/api/v5/docs](https://xmsdwl.nextsls.com/api/v5/docs) | `f896edb6feb6e7035dec31e009be0964b91780372183df0fe5b8eb4312a5f42a` |
| 11 | 美供通 | [https://mtjgyl.nextsls.com/api/v5/docs](https://mtjgyl.nextsls.com/api/v5/docs) | `b8dbfb4edffbeef07c937a81a7b97c3a2b20b72ac85ce8f9a3b7ee9937561b74` |
| 12 | 诺派星云供应链 | [https://sznpxy.nextsls.com/api/v5/docs](https://sznpxy.nextsls.com/api/v5/docs) | `8ffbcad8778b6f1e8d8782bb112b10002b75491752fd75f96f87d40e63d3b433` |
| 13 | 锦星 | [https://zjmrzx.nextsls.com/api/v5/docs](https://zjmrzx.nextsls.com/api/v5/docs) | `247c4f6836da2e3566f784837f29886af9ff14abc8639829b72040084fe5a700` |
| 14 | 顺为 | [https://szswxx.nextsls.com/api/v5/docs](https://szswxx.nextsls.com/api/v5/docs) | `49bbee41fae481ebee8975fa0ae85bb4cd0e4d634c09f31b78e80bafc917ab50` |

## A. 共用协议

- Bearer App Token；使用当前代理自己的 HTTPS 域名，POST JSON。App Key、客户代码及旧轨迹配置保持独立。
- `shipment.service` 采用当前账号 get_services 返回的 code，销售服务名称不作为供应商渠道。
- `shipment.parcel_count`、`to_address`、`parcels` 为创建基本结构；地址 name/city/country/postcode/address_1 是通用必填。发件地址存在时也必须满足地址结构，是否需要填写还需渠道规则。
- `parcels[].number` 是箱号，`declarations` 是本箱商品申报。中英文品名是通用必填。客户/FBA 号、箱号、PO 与 Amazon Reference ID 不混用。
- `declarations[].photo_url` 为 String 图片链接；`photos` 为 String 图片字段，12 家例子都是空字符串。字段类型一致不能证明非空编码一致。搏诚另有技术确认及 BC081190 真实成功依据，使用原始 HTTPS URL；其他租户保持渠道级保护。
- `weight` 为单个商品重量，`size` 为商品长*宽*高，不把箱重或箱尺寸冒充产品数据；SKU、ASIN、FNSKU、销售链接、税号及磁性均必须来自真实资料。
- 有创建附件定义的文档采用 `shipment.attachments[]`，每条包含 name、url、file_type；URL 是 http 开头且最多500字符。现代文档明确 `file_type=order` 为下单文件。第20章的 URL/Base64 不擅自替代创建附件协议。
- 成功按 `status=1` 和 `data.shipment.shipment_id` 保存供应商真实单号。未知结果保持锁定，用当前账号的 shipment/list 按唯一客户参考号对账；无匹配也不自动重发。

## B. 渠道额外要求

现代 get_services 文档提供 require_declaration_list、from_address（optional/required/no）、电话/邮箱/公司/城市/VAT/EORI/装箱单等标志。这些是动态账号渠道数据，创建引擎只有一套，不按供应商复制。只上传当前渠道要求的图片或装箱单；只发送要求且有真实来源的补充申报值。

文档把返回规则列为可选，并没有说明“字段缺失等于不要求”。程序区分明确的0、1与未知值；整数及字符串0/1均支持。weight_required/volume_required 的差异由已有真实箱重尺寸校验覆盖；不要求额外无关资料。

## C. 程序限制与本轮处理

管理员日常开放创建由数据库站点许可控制，页面有权限、CSRF、版本及操作历史；单票授权、防重、超时锁定均保留。部署层紧急停止可以强制关闭。中威的停用是管理状态，本轮明确获准恢复，不作为永久协议阻断。

已知 photos 编码器本身是统一的 HTTPS URL 校验函数。租户确认范围是证据边界，并非每家一套图片上传代码。尚无官方证据可把搏诚确认自动扩展到其他租户，本轮没有为了数量而新增未经证明的编码登记。

## D. 确实缺失的信息及文档差异

锦瞬达（#5）和美供通（#11）的当前 get_services 文档属于旧版本：服务章节编号为10，而非11；只列基础渠道属性。原始JSON每行只有 code/id/label_code/name/type/volume_weight_radix。102+38条均没有 require_declaration_list、from_address、packing_list_required、tel_required、email_required、company_required、city_required、vat_number_required、eori_required。没有可确定这些缺项的默认值，也没有文档支持的另一条渠道规则查询接口。创建文档明确不同服务有不同申报要求。因此不能把未知列表改成空列表或把未知标志改成0。

这两家旧文档还缺少现代创建附件的 file_type 行；仅有链接/名称并不足以证明特定必填装箱单的类型。其他文档的基础创建字段相同，实际差异包括：搏诚自定义预计配送时段；壹合的TM版本自定义项；蓝图梦的同行虚拟单号；亿阳/锦星/顺为的 spid/nspid/mpid；诺派 WMS 对象；梁鑫额外报关方式1001。上述可选业务扩展不自动混发、不阻断未要求它们的普通渠道。

仍待确认 photos：中威4条、亿阳9条、梁鑫169条、诺派星云17条，共199条。相同 String 声明和空示例不能证明 URL、JSON或文件ID中哪一种非空编码正确。其余渠道不受此保护影响。

## 可交给供应商的最小问题

锦瞬达/美供通：请为每个渠道 code 提供 V5 创建规则：require_declaration_list（数组）、from_address（optional/required/no）及 packing_list_required、tel_required、email_required、company_required、city_required、vat_number_required、eori_required（0/1），明确缺失字段默认含义。请确认由 get_services 返回、另一正式只读接口返回或提供带版本的完整渠道规则表，并确认创建时装箱单附件的字段和类型。当前响应只有六个基本键，无法据此判断实际必填条件。

中威/亿阳/梁鑫/诺派：请确认本租户 V5 创建 `parcels[].declarations[].photos` 是否直接接收单张原始HTTPS图片URL（含签名查询参数），并给出一个脱敏的非空成功请求示例；若不是，请提供确切字符串编码及原生上传步骤。无需提供任何账号密钥。
