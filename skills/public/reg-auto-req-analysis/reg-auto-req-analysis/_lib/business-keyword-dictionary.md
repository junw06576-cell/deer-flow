# 业务关键词字典（KB 触发与查询词源·两阶段共享）

> **这是什么**:产品业务概念 → {同义词 / 旧称 / 代码注释用语 / 拼音缩写 / 英文领域名 / 关联子系统·仓库·表前缀} 的映射,用于:
> 1. **扩 KB 触发面**:需求触及本字典任一概念(标准名/同义词/旧称/缩写任一字面或近义)**或**高风险 6 类时,跑 KB/wiki/db 发现链——不再限于 `结算/费用/医保/上传/校验/报表/接口/明细/账单` 这 9 词封闭枚举。
> 2. **`embeddings=0` 下的查询词构造**:GitNexus 当前全部仓库 `embeddings=0`(实测),中文语义召回弱;查询词须按 `中文同义词 → 拼音缩写 → 英文领域名 → API/类/方法/表前缀` 顺序扩展,本字典是这些扩展词的来源。
>
> **初稿来源**:由扫产品 wiki(`wiki/`)+ GitNexus 仓库清单得出,**拼音缩写/英文领域名列系据 wiki 子系统名与仓库名 best-effort 推断,需业务/研发校正**。字典之外的真实业务词 AI 仍可识别(非封闭清单)。
>
> **读时机**:KB/wiki/db 发现链触发时(`knowledge-base.md` §五、`wiki-kb-recipe.md` §1、`qc-kb-recipe.md`);构造 GitNexus/db 查询词时(`module-location-recipe.md`)。归属存疑时参照 [`references/project-keyword-dictionary.md`](../references/project-keyword-dictionary.md)(那是项目归属字典,与本文件用途不同)。

## 一、门诊域（mz_ / opd）

| 标准名 | 同义词 | 旧称/口语 | 拼音缩写 | 英文/子系统 | 关联仓库·表前缀 |
|---|---|---|---|---|---|
| 门诊挂号 | 挂号、预约挂号、号源、排班、接诊、就诊 | 挂号处 | `mz_gh` / `MZ_GHXX` | `registration` / `bathis-mzfy` / `his-mzfy-frame` | bathis-v5.5 · opd 域 |
| 门诊就诊上下文 | 就诊、encounter、就诊流水 | 看诊 | `mz_jz` | `encounterId` / `gp-outpat-cis` | cis 域 |
| 门诊收费/结算 | 收费、结算、待收费、收费结算 | 计费、收费处 | `mz_sf` / `MZSF` | `mzfyjs` / `clinicCharge` / `his-mzfy-frame` | fee 域 |
| 门诊日结 | 日结、结账、结账汇总 | 轧账、对账 | `mz_rj` | `saveMzrj` / `dayendquery` | fee 域 |
| 退费/红冲 | 退费、红冲、被红冲、退号 | 冲红、冲账 | `mz_tf` | `cancelMzfyjs` / `mztfyjs` / `refundoperation` | fee 域 |
| 费别 | 费别、费用类别、医保类型 | 自费/公费/医保 | — | `fb` / 费别字段 | fee 域 |
| 电子发票/票据 | 电子发票、票据、发票号、纸质票 | 发票、小票 | `mz_fp` | `clinicElecInvoicePush` / `elecInvoice` / `bathis-jcsj-frame` | fee 域 |
| 第三方支付 | 微信/支付宝/一码付/信用支付/第三方 | 在线支付、移动支付 | — | `thirdPay` / `thirdRefund` / `getThirdPayMentWay` | fee 域 |
| 门诊处方 | 处方、开方、西药处方、草药处方 | 方子、开药 | `mz_cf` / `MZCF` | `MzCfController` / `west_drug_order` / `cis-frame` | orders 域 |
| 门诊医嘱 | 医嘱、门诊医嘱、下达 | 开医嘱 | `mz_yz` | `mzys` 模块 / `clinical_order` | orders 域 |
| 门诊发药 | 发药、发药确认、配药 | 发药窗口 | `mz_fy` / `Mzfy` | `MzfyController` / `execMzfy` / `batmed-yfyk-fyty-frame` / `boot-batmed-yfyk` | pharmacy 域 · batmed-v5.6 |
| 门诊退药 | 退药、退药回退 | 退药窗口 | `mz_ty` / `Mzty` | `MztyController` / `saveMzty` | pharmacy 域 |
| 药品追溯码 | 追溯码、扫码、码追溯、补码 | 药码、追溯号 | `zsm` | `saveZsm` / `getZsm` / 16·20 位码 | pharmacy 域 |
| 配药单/摆药 | 配药单、摆药、处方打印 | 打单 | `pyd` | `getPyd` / `updatePyzb` | pharmacy 域 |
| 药房/药库 | 药房、药库、中心库、库存 | 药房窗口 | `yp_` / `yk_` | `batmed-yfyk` / 库存管理 14 菜单 · 参数 C113 | pharmacy 域 · batmed-v5.6 |
| 门诊病历 | 病历、门诊病历、病历书写 | 病案(门诊) | `mz_bl` / `MZBL` | `boot-mzbl` / `batemr-mzbl`(端口 9023) · NETEMR 库 | emr 域 · batemr-v5.5 |
| 三级模板 | 模板、全院/科室/个人模板、病历模板 | 模板维护 | `mz_blmb` | `NETEMR_MZBLMB` / 分类 0·1·2 | emr 域 |
| CDSS | 临床决策支持、智能问诊、诊断推荐、质控提醒 | 智能辅助 | — | `proofOutPatDis` / `getCdssInformationPro` · 参数 D132 | emr/cis 域 |
| 传染病上报 | 传染病、公卫上报、自动上报 | 疫报 | — | `NETEMR_NEMR_CRB` / `judgeInfectiousDis` · 参数 M021 | public_health 域 |
| 互联网医院 | 互联网医院、线上问诊、病历回写、卓健/海旅/讯飞 | 线上诊疗 | — | `OutPatientRpcController` / `/rpcService/` | portal 域 |
| 皮试/输液/注射 | 皮试、输液、注射、输液座位、护理执行 | 打针、输液室 | — | `saveMzPsJG` / `infusion_order` | orders/cis 域 |

## 二、住院域（zy_ / inpatient）

| 标准名 | 同义词 | 旧称/口语 | 拼音缩写 | 英文/子系统 | 关联仓库·表前缀 |
|---|---|---|---|---|---|
| 入院登记 | 入院、入院登记、住院登记、住院号、首页序号 | 办住院 | `zy_rydj` / `RYDJ` | `saveCheckInRegister` / `his-zyfy-frame` / `syxh` | inpatient 域 |
| 首页序号 syxh | syxh、住院首页序号、首页号 | 住院流水号 | `syxh` | 费用/预交金/结算核心键 | inpatient·fee 域 |
| 费用限额/停药线 | 停药线、报警线、费用预警、欠费停药 | 限额 | `yjdx`/`yjbj` | `yjdx`/`yjbj` 字段 · 参数 C228 | inpatient·fee 域 |
| 住院病历 | 住院病历、入院记录、病程记录、出院记录 | 病案(住院) | `zy_bl` / `ZYBL` | `batemr-zybl` / `/zybl/medicalWrite` / `/batemr-zybl/emrService` | emr 域 · batemr-v5.5 |
| 病案首页 | 病案首页、病案信息、首页 8 页签 | 病案 | `ba_sy` / `BA` | `/zybl/patientInfo` / `modifyInpInfo` / `inpInfo` | emr 域 |
| 病历审核状态 | 提交/审核/退回/锁定/归档/召回 | 病历状态 | — | 状态 0/1/2/3 · `/examine/*` | emr 域 |
| 医嘱录入 | 医嘱、长期医嘱、临时医嘱、出院带药、首日医嘱、成套医嘱 | 开医嘱 | `yj_` / `YJ` | `YizhuService` / `/cis-frame/#/medicalOrderEntry` · 状态 0-9 | orders 域 · batcis-v5.5 |
| 医嘱校对/执行 | 校对、执行、停止、医嘱状态机 | 核对医嘱 | — | `yzxxProofread` / `medicaladviceexecute` · 1→2→3→4 | orders 域 |
| 住院发药 | 住院发药、领药、摆药、出库 | 病区领药 | `zy_fy` / `Zyfy` | `execZyfy` / `/zyfy/*` / `batmed-yfyk-base-frame` | pharmacy 域 · batmed-v5.6 |
| 住院退药 | 住院退药、退药入库、家床退药 | 退药 | `zy_ty` / `Zyty` | `execZyty` / `/zyty/*` · `djlx=2/3` | pharmacy 域 |
| 库存冻结/核销 | 库存冻结、批次预留、核销、防负库存 | 锁库存 | — | 冻结批次扣减 / 释放 | pharmacy 域 |
| 预交金 | 预交金、押金、预存、预交金退费 | 押金 | `zy_yjj` / `YJJ` | `savePrePayData` / `/zybryjj/refund` / 原单红冲 | fee·inpatient 域 |
| 出院结算 | 出院结算、在院结算、轧差结算、结算服务工厂 | 结账出院 | `zy_js` / `jszt` | `saveSettlement` / `checkPreSettlement` · `jszt=1/2` | fee 域 |
| 预结算校验闸 | 预结算、结算校验、未发药阻断 | 预结 | — | `checkPreSettlement` / `medicare/drg/hospSettlementCheck` | fee·insurance 域 |
| 住院日结 | 住院日结、日终结账、未缴款/已缴款 | 轧账 | `zy_rj` | `saveDailySettlementData` / `InHospitalDailySettlementService` | fee 域 |
| 住院护理/护士站 | 护士站、住院护理、病区、床位、入出区、换床换区 | 护理站 | `zy_hl` / `ZyHsz` | `cis-lchl-frame` / `nurse-station` / `/zyhl/ZyHsz` · `/zyhl/ZyBq` | inpatient·qc 域 |
| 护理文书 | 护理记录、护理文书、生命体征、评估、体温单 | 护理单 | — | `hlws` / `region-batemr-web-hlws` / `/nursemedicalwrite` | emr 域 |
| 不良事件 | 不良事件、安全事件、上报、三级角色 | 事件上报 | — | `froview-bav` / `blfy` / `zhfw` / `adverseEvent` | qc 域 |
| 护理中心 | 护理中心、护理管理、综管大屏、驾驶舱 | 护理部 | — | `ncms-web` · 菜单 `24_0001xxxx` | mgmt 域 |

## 三、医保结算域（yb_ / insurance）

| 标准名 | 同义词 | 旧称/口语 | 拼音缩写 | 英文/子系统 | 关联仓库·表前缀 |
|---|---|---|---|---|---|
| 医保结算 | 医保、医保结算、医保预结算、正式结算、统筹、个人账户 | 医保报销 | `yb_` / `YB` | `ybyjsAfterBuild`/`ybjsAfterBuild` / `medicare` | insurance 域(337 表) |
| 医保上传/目录对照 | 医保上传、目录对照、医保接口、报销类别 | 医保对照 | — | 28 号入院 / 29 号撤销交易 · 就诊类型/报销类别 | insurance 域 |
| DRG/DIP | DRG、DIP、病种分组、预分组、医保分组 | 分组 | — | `drg/hospSettlementCheck` / `inpatientCheck` · 参数 E302/D152 | insurance 域 |

## 四、检查检验域（exam/lab）

| 标准名 | 同义词 | 旧称/口语 | 拼音缩写 | 英文/子系统 | 关联仓库·表前缀 |
|---|---|---|---|---|---|
| 检查 | 检查、检查申请、检查报告、RIS | 检查科 | `jc_` | `exam` / RIS 接口 · 申请单模板 | exam 域 |
| 检验 | 检验、LIS、样本、检验报告、重复校验 | 化验 | — | `lab_test_item_check_duplicate` / `LisGkController` | lab 域 |

## 五、平台/基础数据域（gy_ / xt_）

| 标准名 | 同义词 | 旧称/口语 | 拼音缩写 | 英文/子系统 | 关联仓库·表前缀 |
|---|---|---|---|---|---|
| 基础数据 | 基础数据、字典、主数据、收费项目、药品目录 | 基础维护 | `gy_` | `batbsc-frame` / `bathis-jcsj-frame` · 菜单 `20_0001xxxx` | base 域(26 表) |
| 网页框架/权限 | 框架、RBAC、权限、用户、角色、机构、科室、灰度、对照 | 底座 | `xt_` | 网页框架 · 菜单 `99_` | system 域(88 表) |
| 系统参数 | 参数、系统参数、开关、参数配置 | 开关 | — | 参数编码(如 A026/B120/C113/D132/E215/M021…) | system/base 域 |
| 临床后端接口 BatCIS | BatCIS、boot-batcis、临床服务、HSB、Feign | 后端服务 | — | `boot-batcis` · 模块 core/mzys/lchl/jkfw · `/hsb/` `/api/` `/rpc/` | batcis-v5.5 等 |
| 物资/材料 | 物资、材料、物资管理、材料目录 | 库房(非药) | — | 物资信息/厂家/类别 · ⚠️wiki `平台/物资管理.md` 链接失效(缺口) | mgmt 域 |
| 阳光采购 | 阳光采购、省平台、配送点、供货厂商、平台单位 | 集采 | — | 配送点管理/平台单位设置/供货厂商对照 | mgmt 域 |
| 人群优惠/惠民 | 贫困人口、优抚、人群优惠、惠民、收费策略 | 惠民政策 | — | 人群优惠设置/人群收费策略 | base·fee 域 |

## 六、数据库表前缀速查（db-knowledge 查表用，详见 wiki/数据库/NETHIS数据库结构总览.md）

| 前缀 | 业务域 | db-knowledge 检索提示 |
|---|---|---|
| `gy_` | 基础字典(科室/员工/收费项目/诊疗项目) | base 域 |
| `zy_` | 住院(入出院/病区/床位) | inpatient 域(107 表) |
| `mz_` | 门诊(挂号/就诊/处方/收费) | opd 域(211 表) |
| `fy_` | 费用(结算/票据/支付) | fee 域(92 表) |
| `yp_` / `yk_` | 药品(药库/药房进销存) | pharmacy 域(225 表) |
| `bl_` | 病历(门诊/住院病历) | emr 域(196 表) |
| `yj_` | 医嘱(录入/校对/执行/停止) | orders 域(104 表) |
| `yb_` | 医保(目录对照/结算上传/DRG) | insurance 域(337 表) |
| `xt_` | 系统(用户/角色/菜单/参数) | system 域(88 表) |
| NETEMR_* | 门诊电子病历(boot-mzbl) | NETEMR 库 |

> 23 个领域完整目录见 `wiki/数据库/NETHIS数据库结构总览.md`;查具体表/字段先定位领域,再经 db-knowledge `get_table_knowledge` 取详情(需精确 `database_alias + table_name`)。

## 七、使用约定

- **触发判定**:需求标题/描述/验收里出现本字典任一概念的字面或近义,**或**命中 [`high-risk-categories.md`](high-risk-categories.md) 6 类 → 跑发现链。判"纯文字无业务实体"而跳过时,必须写明"对照字典检查了 X/Y/Z 概念、均未命中",记 `kb.note`。
- **查询词构造顺序**(embeddings=0):① 中文标准名+同义词 → ② 拼音缩写/英文领域名(本字典) → ③ 从 wiki/菜单/API 提取的 Controller/方法/DTO/表名精确锚点 → ④ 仍空按 `module-location-recipe.md` 的回退(cypher/impact/traverse/换 repo)。
- **非封闭**:字典未列的真实业务词(新模块/客户特有)AI 仍可识别并触发;发现遗漏请补登本文件。
- **db 联查**:从业务概念推表名须经"业务词 → 代码字段/Mapper/SQL → 正式表列"闭环,不准直接猜表名(见 `module-location-recipe.md §6`)。
