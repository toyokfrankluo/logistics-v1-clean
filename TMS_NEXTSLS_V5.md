# 星睿 TMS 与 NextSLS V5

已通过用户打开的浏览器核对 [BCKJKJ 租户文档](https://bckjkj.nextsls.com/api/v5/docs) 的 20 章。这里的「文档确认」指端点或字段有说明；已在该租户对一票既有运单完成真实只读验证。渠道必填规则、接口权限、字段单位及费用归属按各渠道实际授权和供应商证据核对。

## 接口能力

下表路径均以 `https://bckjkj.nextsls.com/api/v5` 为前缀。除第 1 章描述的认证方式，其余均使用 POST JSON。请求发送 `Authorization: Bearer <ACCESS_TOKEN>`，另发 JSON 类型和可选的 `Accept-Language: zh-CN`。文档由客服或账号的 API 密钥设置提供 Token，各能力的账号权限并未公开列出。通用返回 `status` 为整数（1 成功，0 失败），`info` 为消息或失败原因，`time` 为可选时间戳。界面只显示本系统生成的错误，不透传供应商正文。

| 章 | 路径 | 请求要点、类型及必填项 | 主要返回 | 权限性质 / 状态 |
|---|---|---|---|---|
| 1 | 无独立登录 API | Bearer Token，每次请求带入 | 通用认证规则 | Token 权限待核实 |
| 2 | `/shipment/create` | `shipment` 对象，渠道代码、箱数、收件地址、箱及申报必填；具体渠道可能另有必填项 | `data.shipment.shipment_id` 字符串；可附换标标志及逐箱标签 | 写入；模拟验证，默认关闭 |
| 3 | `/shipment/get_labels` | `shipment` 中的系统单号或客户参考号，字符串 | 系统标签 URL 列表、转单号、逐箱标签或换标信息 | 读标签，权限未验；仅配置 |
| 4 | `/shipment/get_tracking_numbers` | `shipment` 中的系统单号或客户参考号，字符串 | 系统单号、承运商代码、承运单号及逐箱转单号 | 读运单，权限未验；仅配置 |
| 5 | `/shipment/void` | `shipment` 中的系统单号或客户参考号，字符串 | 系统运单号 | 写入取消，权限未验；仅配置 |
| 6 | `/shipment/get_tracking` | 文档支持多种标识；自动配置用本票已关联的系统单号及 `language=zh` | `data.shipment`：单号、状态、承运商及路由数组；示例时间为 Unix 秒，字段表写字符串 | 读轨迹；自动配置默认关闭，旧代理保留原配置 |
| 7 | `/shipment/update_weight` | 运单标识、`parcels` 数组；复用箱对象，客户重量 kg 必填，尺寸 cm 按渠道规则填写 | 系统运单号 | 写入客户申报；不可用来查询实测 |
| 8 | `/shipment/update_declarations` | 运单标识、箱数组及申报数组；箱号与中英文品名按箱对象定义 | 系统运单号 | 写入申报；本轮未开放 |
| 9 | `/shipment/get_info` | `shipment` 中的系统单号或客户参考号；同步使用系统单号 | 运单、收货箱数、计费重、材重、体积、逐箱计费字段、费用列表等 | 读运单及可见费用；模拟验证，默认关闭 |
| 10 | `/shipment/get_waybill` | `shipment` 中的系统单号或客户参考号 | `shipment.service` 与 `shipment.waybill` 对象；提单类型、航程时间、地点 | 读提单，权限未验 |
| 11 | `/shipment/get_services` | `services.type` 字符串；示例 `all` | `data.services` 数组，包含渠道代码、规则、材积系数及要求的申报项 | 读渠道；下单前查询 |
| 12 | `/shipment/get_account` | 示例空对象 | `data.account` 名称、币种、余额与欠款均为字符串 | 读账户资金；权限未验，不能作为单票结算 |
| 13 | `/shipment/get_rate` | `shipment` 对象；渠道、箱数、收件地址（含电话）、箱与申报必填 | `data.charge`、`currency`、`info` 字符串 | 试算，不能作为最终应付 |
| 14 | `/shipment/calculator` | `shipment` 对象；发货区、目的国及箱数组必填，箱重及长宽高为数字 | 报价数组：服务代码、名称、时效、明细价、总价、说明字符串 | 试算；本轮未接入 |
| 15 | `/shipment/address` | `address` 对象，代码及渠道字符串可选，分页为数字 | 地址对象和分页；地址含字符串国家代码与邮编 | 读地址库，权限未验 |
| 16 | `/shipment/list` | `shipment` 中可按系统单号或客户参考号、状态、时间范围、页码与页长筛选；对账传唯一客户参考号 | 示例 `data.shipment` 为对象数组，系统运单号、渠道、客户参考号、状态及箱信息 | 读列表；模拟验证，默认关闭 |
| 17 | `/user/login_url` | 空对象 | `data.url` 字符串；一次性登录凭证 | 产生敏感登录链接；不接入 |
| 18 | `/shipment/check_remote` | `shipment` 中的渠道和收件地址，国家及邮编必填 | `data.remote`；例为数字，字段表写字符串 | 读偏远标志，权限未验 |
| 19 | `/shipment/delivery` | `delivery` 中的运单标识，加小包标签或大货逐箱信息；系统箱号、承运商代码及转单号按类型必填 | 通用状态 | 写入标签；未开放 |
| 20 | `/shipment/upload_attachments` | `shipment` 内运单标识及附件列表，文件名、类型及 URL 或 Base64 | 通用状态 | 写入附件；未开放 |

端点路径、必填字段和示例可在[租户原文](https://bckjkj.nextsls.com/api/v5/docs)相应章节复核。没有批量创建协议或服务端幂等键说明。第 2 章正文将 `parcel_count` 列为字符串，示例写数字；适配器按字段表发送字符串，须以测试租户验证。海关编码保持字符串，避免丢失前导零。下单响应即使 `status=1`，缺少有效系统单号仍视为结果未知并进入对账。

## 后台配置一个 NextSLS 账号

1. 在「代理管理」填写代理名称，选择 `NextSLS V5`，只填写供应商给出的 HTTPS 基础域名，不加 API 路径。新代理可留空「原有轨迹 API 地址」。迁入自动配置的旧 NextSLS 代理可保留同租户地址，但自动轨迹不会回退到该地址；要保留原有模式，请维持原 API 类型。
2. 在 `App Token` 填写此租户的令牌，编辑留空保留旧值。NextSLS V5 以 Bearer Token 认证，不使用 App Key 或客户代码。其他旧代理确有需要时，再填写对应字段。切换租户须填写新租户的 Token。
3. 「已核对的代理渠道代码」填写此账号可用的代码，以空格分隔。审核时从所选代理的代码中选择。客户 Excel 中的星睿销售服务独立保留。发件地址按渠道要求补齐。
4. 勾选轨迹、重量体积、逐箱计费明细及代理费用中已获账号授权的功能。后三项共用一次 `get_info`。如要处理下单超时，可勾选「查询创建结果」。写入类接口保持关闭，待单独审批。
5. 编辑页显示当前端点、账号配置、勾选状态、系统开关和最近一次真实验证记录。模拟测试不会标记真实验证。开启系统级只读开关后，管理员用已关联系统单号的运单成功查询可记录轨迹验证时间；已有公开验证报告不会写入新代理记录。取消、标签、结算等尚未接入的能力不会发请求。匿名轨迹入口的保护独立保留。

## Excel → 星睿字段 → NextSLS

以 11.6 版星睿物流发票模板及实际导入代码为准。上传时从发票内置地址库匹配 L4，保存待审核收件资料。后续下单使用管理员确认后的地址。客户号、星睿订单 ID、供应商号、承运号保持各自含义。

| 发票输入 | TMS | 创建请求字段 |
|---|---|---|
| D4 客户/FBA 订单号 | `invoice.customer_number`、`Shipment.tracking_number` | `shipment.reference_1`；系统单独生成 `client_reference` 供唯一对账 |
| D5 运输服务 | `invoice.service` | 保留为星睿销售服务；`shipment.service` 使用审核所选且代理配置确认的渠道代码，下单前比对第 11 章 |
| D6 发货公司 | `metadata.sender` | 可补充已配置发件地址的公司；完整发件地址按渠道另填 |
| D7–D10 特殊属性 | `flags` | `attrs` 中的带电、带磁、液体、粉末 |
| D11–D13 报关、清关、交税 | `metadata` 对应字段 | `exportwith`、`importwith`、`taxwith`；仅转文档中已识别的选项 |
| D14 PO | `metadata.po_number` | `reference_2`；不推断为 Amazon Reference ID |
| D15 箱数 | 导入时核对实际箱数 | `parcel_count` |
| D16 VAT/EORI | `metadata.vat_eori` | 通过 `VAT:` 或 `EORI:` 前缀分别进入 `vat_number` 或 `eori` |
| D17 产品备注 | `metadata.product_note` | `remark` |
| L4 / 地址库 B | `address_code` | 本地地址查找键；不作为 FBA 仓库代码发送 |
| 地址库 D | `address.warehouse_code` | 已提供时发送 `to_warehouse_code` |
| 地址库 E–O / L16 邮件 | `invoice.address` | `to_address.name/company/tel/mobile/address_1/address_2/address_3/city/state/country/postcode/email` |
| A23–A52 箱号 | `boxes[].box_number` | FBA 箱号按文档作为 `number`；一般箱号保留在 `reference`，`number` 使用顺序号 |
| B–H、N 产品申报 | `declarations` | 按箱归组，写入 `name_en/name_zh/material/usage/hscode/unit_value/qty/is_battery`；币种 `USD` |
| I 申报总价 | `total_usd` | 本地复算；接口以单价和数量为准 |
| J–M 箱重及尺寸 | 客户申报 `boxes` | `client_weight/client_length/client_width/client_height`；kg/cm |
| O 图片、原文件 | 私有导入附件 | 保留在原发票中。NextSLS 的 `photos` 字符串编码及 `photo_url` 能否满足渠道要求仍待确认 |

在管理员订单详情的「供应商下单 → 补充 NextSLS 下单资料」，逐条申报补充真实的 `amazon_ref_id`、`brand`、`model`。Amazon 参考号从对应 Amazon 发货资料取得，`reference_2` 继续使用发票 PO。补充资料绑定订单选定的代理和渠道。

第 2 章创建请求支持 `shipment.attachments` 中的文件名、类型和链接。第 20 章上传附件支持 URL 或 Base64，但要求已有可识别的运单。对要求装箱单的渠道，系统先拦截。供应商确认接受创建请求内的 `file_type=order` 附件作为该渠道装箱单后，才可在 Render 配置 `TMS_NEXTSLS_PACKING_ATTACHMENT_HOST` 为获准的 HTTPS 文件域名，并为授权订单补充文件名和文件链接。

## 实测、费用与利润

- 运单详情保存允许的供应商原字段及规范化数值，逐箱保存 `actual_weight`、计费量与计费长宽高。快照变更保留历史，客户原始申报保持独立。手工维护的供应商单号也须与接口返回的客户单号同时匹配。
- `actual_volume` 保留原值。确认其在本票表示实重时才使用 kg。计费量按本票代理渠道确认 kg 或 m³。总体积和逐箱尺寸未确认单位时显示待确认。材积重零值标记为含义待确认。测量值或对应箱号变化后，按新快照重新核对。
- `charge_list` 返回的金额、币种、单价、单位、汇率和业务时间单独展示。采购人员确认归属、预估或实际、结算状态、应付和其他成本。跨币种填写汇率和日期，折算方向为每一单位采购币种对应的应收币种金额。费用变更后保留旧确认并暂停利润，重新复核后恢复。历史确认缺少新字段时也须复核。
- 管理员可以导出内部测量与利润。客户报表仅含经核对的测量值和可唯一匹配到发票的客户箱号。日常应收账单仅用客户单号。原始发票模板保持三工作表结构。

## 开发分支与迁移

新增 `tms_agent_api`、`tms_supplier_order`、`tms_supplier_event`。复用现有代理密钥、原轨迹地址、已审核订单及其唯一 Shipment。新表通过 `python -m flask --app app tms-upgrade-v1 --confirm` 增量创建。先核对独立 Render 测试数据库目标并备份；应用启动不会自动迁移。迁移验证使用隔离 SQLite。PostgreSQL 在独立副本复核后再部署，回退时保留新表及审计。

默认不开启 `TMS_NEXTSLS_READ_ENABLED` 或 `TMS_NEXTSLS_CREATE_ENABLED`。经授权，用新 Token 核对渠道、账号创建权限和只读对账。真实测试须将 `TMS_NEXTSLS_CREATE_ORDER_ID` 设为唯一授权订单 ID，启用只读对账，然后才打开创建开关。后台该票须单独确认。提交请求前保存唯一记录。超时或异常时按唯一参考号对账，经供应商确认未创建并记录依据后才可解除锁。测试结束时将创建开关恢复为 `0` 并移除单票 ID。租户变化时须人工核对原提交记录。
