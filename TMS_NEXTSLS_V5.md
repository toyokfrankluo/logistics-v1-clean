# 星睿 TMS 与 NextSLS V5

已通过用户打开的浏览器核对 [BCKJKJ 租户文档](https://bckjkj.nextsls.com/api/v5/docs) 的 20 章。这里的「文档确认」指端点或字段有说明；没有使用代理账号发业务请求。渠道必填规则、接口权限、字段单位及费用归属须以租户实际授权和只读响应确认。

## 接口能力

下表路径均以 `https://bckjkj.nextsls.com/api/v5` 为前缀。除第 1 章描述的认证方式，其余均使用 POST JSON。请求发送 `Authorization: Bearer <ACCESS_TOKEN>`，另发 JSON 类型和可选的 `Accept-Language: zh-CN`。文档由客服或账号的 API 密钥设置提供 Token，各能力的账号权限并未公开列出。通用返回 `status` 为整数（1 成功，0 失败），`info` 为消息或失败原因，`time` 为可选时间戳。界面只显示本系统生成的错误，不透传供应商正文。

| 章 | 路径 | 请求要点、类型及必填项 | 主要返回 | 权限性质 / 状态 |
|---|---|---|---|---|
| 1 | 无独立登录 API | Bearer Token，每次请求带入 | 通用认证规则 | Token 权限待核实 |
| 2 | `/shipment/create` | `shipment` 对象，渠道代码、箱数、收件地址、箱及申报必填；具体渠道可能另有必填项 | `data.shipment.shipment_id` 字符串；可附换标标志及逐箱标签 | 写入；模拟验证，默认关闭 |
| 3 | `/shipment/get_labels` | `shipment` 中的系统单号或客户参考号，字符串 | 系统标签 URL 列表、转单号、逐箱标签或换标信息 | 读标签，权限未验；仅配置 |
| 4 | `/shipment/get_tracking_numbers` | `shipment` 中的系统单号或客户参考号，字符串 | 系统单号、承运商代码、承运单号及逐箱转单号 | 读运单，权限未验；仅配置 |
| 5 | `/shipment/void` | `shipment` 中的系统单号或客户参考号，字符串 | 系统运单号 | 写入取消，权限未验；仅配置 |
| 6 | `/shipment/get_tracking` | `shipment` 中的系统单号、客户参考号或承运单号；箱号、提单号、扩展箱号与语言可选，均为字符串 | `data.shipment`：单号、状态、承运商及路由数组；示例时间为 Unix 秒，字段表写字符串 | 读轨迹；旧实现继续使用原配置 |
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

## Excel → 星睿字段 → NextSLS

以桌面的 11.6 版星睿物流发票模板及实际导入代码为准。仅检查结构和表头。收件资料由客户授权的地址库解析。客户号、星睿订单 ID、供应商号、承运号保持各自含义。

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
| O 图片、原文件 | 私有导入附件 | 保留本地。没有授权的图片传输或附件 URL；需要图片或装箱单的渠道会阻止提交 |

## 实测、费用与利润

- 第 9 章可读 `picked_count`、`chargeable_weight`、`volume_weight`、`volume` 和箱 `number`、`ext_number`、`chargeable_*`。示例计费值有数字字符串。系统按供应商箱号保存一份当前快照，变更时保留历史。供应商回传的计费数据不会改写客户原始申报。
- 文档将 `actual_volume` 标为运单实重，字段名与说明矛盾；实现保留未确认原值，不用它填写实重或体积。未发现独立逐箱扫描、仓库实重、收货重或测量时刻端点。逐箱尺寸是计费尺寸；供应商租户的计量单位需核对。第 11 章的 `volume_weight_radix` 是渠道配置，不能证明历史运单实测使用该系数。
- `charge_list` 有费用类型、金额、币种、业务时间、单位、单价、汇率。没有找到能证明费用归星睿账号应付、为最终结算，或逐票结算状态的接口。第 12–14 章余额与试算不能替代该证据。
- 新同步费用先作为供应商返回的原始计费口径，采购人员核对后录入应付、汇率、其他成本和人工确认的结算状态。折算方向为「每一单位采购币种对应的应收币种金额」。使用 Decimal；未知成本不显示利润，费用变化或应收币种变化后须复核。客户页只展示客户应收。
- 管理员可导出运单汇总、逐箱计费 Excel 和内部单票利润 Excel。此次提交的附件仅有需求文字；没有独立的仓库报表截图可用于逐格比对。报表样例业务值未写进代码或测试。

## 开发分支与迁移

新增 `tms_agent_api`、`tms_supplier_order`、`tms_supplier_event`。复用现有代理密钥、原轨迹地址、已审核订单及其唯一 Shipment。新表通过 `python -m flask --app app tms-upgrade-v1 --confirm` 增量创建。先核对独立 Render 测试数据库目标并备份；应用启动不会自动迁移。迁移验证使用隔离 SQLite。PostgreSQL 在独立副本复核后再部署，回退时保留新表及审计。

默认不开启 `TMS_NEXTSLS_READ_ENABLED` 或 `TMS_NEXTSLS_CREATE_ENABLED`。经过独立授权后，先核对账号 Token 权限、已知运单和所用渠道，再开启对应的只读验证；确认客户数据范围及供应商测试渠道后才考虑开启创建。后台每票须单独确认。提交请求前保存唯一记录；超时或异常不自动重试。先按唯一参考号查询，再由管理员记录供应商未创建的依据、解除锁，并重新确认下单。租户发生变化时需人工核对原提交记录。
