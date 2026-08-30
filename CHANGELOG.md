# PDD EZ 更新日志

## v1.5.11（报错提示体系：全量审查 + 分类/文案/动作规范落地）

**用户痛点：①配 OCR 专用副模型时提示"无效仅单模型"困惑；②主副模型相同提示被误读为失败。拉队全量审查（96KB 双文档）后体系化改造**：

- **错误分类扩展**（`ocr_review`）：新增 `model_not_found`（1211/InvalidEndpointOrModel/模型不存在——**置于 fatal_quota 之前**，模型名打错不再误导去查余额）、`dual_config_invalid`、`api_unreachable`、`no_table_detected` 四分类；`fatal_quota` 标题改「API 权限或额度」
- **双模型交互再设计**（用户两痛点闭环）：
  - 副模型=OCR 专用（qwen*-ocr）：识别照跑单模型（数字仍准），状态栏明确「设计跳过**非失败**」+ 引导"想双验证请把主副都换 VL 模型"（新标记 `_dual_skipped_ocr` 区别于失败 `_dual_degraded`）；设置页副模型输入实时显示能力说明
  - 主副模型相同：**不再空耗双模型 API**（识别前即降级单模型）+ 状态栏引导"配置不同的副模型（设置→API 管理）"
- **预检扩展**：`api_config_status` 增副模型诊断（ocr_only/same_as_main/none），识别入口以状态栏建议呈现（不阻塞识别）
- **文案归一**：空结果三处统一「未识别到表格数据——请确认截图包含完整表格」；泛化『出错』类提示全项目清零
- **审查产出**：`docs/BUG_HUNT_ERRORS.md`（88 处弹窗 + 86 处状态栏盘点，P0 清单 11 条已闭环）+ `docs/ERROR_SYSTEM_DESIGN.md`（940 行：24 分类总表/弹窗动作规范/限流规则，GUI 层 show_actionable_error 规格存档待后续）
- **附带**：regions.json 落库用户真实识别数据（山东地区——识别链路 v1.5.10 修复后已真实可用）

**测试总账**：**1030 项全绿**（v1.5.10 的 1000 + test_error_system 30 断言：新分类命中/常量完整性/优先级/无泛化提示）。

## v1.5.10（致命修复：识图/导入从未真正执行）

**用户多轮反馈「点识别即报错、压根没开始识别」→ 复现定位到致命签名 bug**：
- **根因**：`_live_screenshot` 与 `_import_table` 内部的任务函数写成 `def task():`（无参），而任务队列契约是 `fn(progress)`——worker 一执行即 `TypeError`，任务立判失败 → 泛化「出错」弹窗。**识别与表格导入从此从未真正开始**（程序化探针复现：`TypeError: task() takes 0 positional arguments but 1 was given`）
- **修复**：两处 `def task(_progress=None):` 签名修正；批量识别（`_batch_task_fn(progress)`）与批量图片（`task(progress)`）核对无恙
- **防回归**：新增 `test_queue_signature.py`（5 用例，源码级断言所有 submit 任务函数必须接受 progress 参数 + 禁止无参 `def task():` 回归）
- 附带本轮诊断闭环：识图全流程步骤日志（开始/截图/OCR 结果）、`_friendly_error` 报错原文入日志、批量无地区首次引导、识图无窗口引导（后续远程诊断可直接读日志定位）

**测试总账**：**1000 项全绿**（v1.5.9 的 995 + 5）。

## v1.5.9（识别按钮 API 预检：报错必说清是"API 未配置"）

**用户反馈「点识别按钮报错，但没提示是 API 的问题」→ 修复提示链路**：
- 识别入口（识图=截当前窗口 / 批量识别 / 批量图片）新增**前置 API 配置预检**（`api_config_status` 纯函数）：active provider 的 API Key（含 DPAPI 密文解密后判定）或识别模型为空 → 弹窗标题直接写 **「API 未配置」**，正文列出缺什么（Key/模型）、去哪填（设置 → API 管理），并提供**一键跳转 API 管理页**按钮——不再让用户对着泛化「识别失败」猜
- `ocr_review` 分类标题改准：`api_key_missing` → 「API 未配置」、quota 类 → 「API 权限或额度」（作为预检兜底的第二道防线，API 层 401/未设置仍能正确归类）
- 新增 `test_api_ready.py` 12 用例（空 key/空模型/DPAPI 解密成败/配置读取失败/未知提供商/跳转联动）

**测试总账**：**995 项全绿**（v1.5.8 的 983 + 12）。

## v1.5.8（导入按钮报错修复 + 全量 bug 审查闭环）

**用户实测「导入按钮报错」→ 全量 bug 审查全量 bug 审查 → 修复闭环**：

**根因（★High×2）**：
- `_open_import_menu` 整体 `try/except` 静默兜底到表格导入——任何 Tk 异常（grab 失败/menu 构造异常）用户感 =「按钮报错→莫名弹出表格导入对话框」；修复：except 收窄为 `tk.TclError` + 状态栏/弹窗**显式**提示（DESIGN §4），其余异常原样冒泡
- `_dispatch_import` 吞 `pick_images` 抛错后静默转表格导入——用户点「选择图片文件」结果弹 CSV 选择框；修复：显式分派（未知 key 记日志忽略），业务异常交由异常守卫/调用方显式呈现

**其他修复（High/Medium 全闭环）**：
- A1：`_import_table` 补批量互斥守卫（`_batch_running`/`_img_batch_running` 双判定，批量运行中禁止导入，同款提示卡）
- A2：`import_btn` 纳入 `_BATCH_BUSY_BTNS`——批量期间按钮禁用 + 「导入中…」（`home_actions` 'import' key 由死字段转活）
- A5：店铺切换 busy 判定补 `_img_batch_running`（v1.5.x R1 引入的跨店数据错位回归）
- A3：`_ocr_fill` 死代码删除（v1.5.7 定版后零调用方）
- F2：导入菜单对象用后即 `destroy()`（防 widget 树累积与 grab 残留）
- F3/F4：except 范围收紧 + 菜单渲染改走 `menu_labels` 契约

**审查产出**：`docs/BUG_HUNT_V157.md`（714 行，含探针证伪模态嵌套假设）+ `docs/REVIEW_IMPORT_MENU.md`（480 行交互深水区）

**测试总账**：**983 项全绿**（v1.5.7 的 953 + 新增 30 项防回归断言：test_import_menu_fix.py 28 + test_home_actions +3）。

## v1.5.7（主页布局定版：批量｜导入｜识图｜导出 / 刷新｜双模型）

**用户拍板定版（替代 v1.5.6 截图合并菜单方案，消除功能重复）**：

1. **批量**：保留一键直达（自动滚动采集整页、多省份独立、双模型/成本预估/F9 均照旧），第一排最左
2. **导入**：升级为文件数据源统一入口——点击弹菜单二选一【导入表格文件】【选择图片文件（1..N 张）】；v1.5.6 的截图菜单解散后，本地图片选择识别**并入导入**（用户：本地图片功能并入导入）
3. **识图**：直连**截取当前窗口**（最小化→截 PDD 窗口→恢复→识别，1 张，含每日门控）——不再弹菜单，与批量（滚全表）分工清晰
4. **导出**：第一排最右（primary）
5. **第二排**：刷新（左）+ 🛡 双模型（右）

**配套**：`home_actions.py` 契约升级（IMPORT_MENU_ITEMS 替代 SHOT_MENU_ITEMS；busy keys 收敛为 import/image/refresh/batch/export/capture）；`_BATCH_BUSY_BTNS` 保持 export/live(识图→image) 两项；删除 `_open_shot_menu/_dispatch_shot`（新增 `_open_import_menu/_dispatch_import`）；「截图」按钮全仓移除（测试断言含防回归）。测试同步：test_home_actions 重写（8 类 82 用例）+ test_smoke t27 布局断言更新 + 防回归断言（无 SHOT_MENU_ITEMS/_open_shot_menu/'截图' 按钮）。

**测试总账**：**953 项全绿**（v1.5.6 的 950 + 3）。

## v1.5.6（主页布局：用户实测三条意见落实）

**用户桌面实测反馈 → 一轮 UI 重构用户实测反馈 → 一轮 UI 重构**：

1. **按钮换位**：「识图」「导出」上移第一排（数据入口+结果动作同排）；「🛡 双模型」勾选下移第二排（操作+选项同排）
2. **识别入口合并**：「实时截图」+「批量图片」合并为唯一「截图」按钮——点击弹菜单二选一：**截取当前窗口（1 张）** / **选择图片文件（1..N 张）**；客户只需决定选几张图。菜单数据来自新 `home_actions.py` 契约模块（单一事实源）；每日免费门控、批量忙互斥、F9 紧急停止对两条路径分别保留
3. **按钮统一尺寸**：主页全部按钮按 `home_actions.BTN_WIDTH_HOME=6` 统一宽度（`btn_width_for(kind)`），截图/导入/刷新/批量/识图/导出视觉对齐

**配套**：`home_actions.py`（菜单/宽度/忙文案纯函数契约，22 用例）+ 忙时段文案接入 `busy_label_for`（截图中…/导出中…）；`_BATCH_BUSY_BTNS` 收敛为 export/live 两项；`img_batch_btn` 全仓零代码引用；审查报告 `docs/REVIEW_LAYOUT.md`（High=0/Medium=0，5 条 Low 建议）；t27 布局源码断言与忙状态表断言同步更新 + 新增 13 用例（忙状态表覆盖/合并入口 busy 拒绝/菜单-gui 接线契约）。

**测试总账**：**950 项全绿**（v1.5.5 的 915 + home_actions 22 + 布局断言新增 13）。

## v1.5.5（自主迭代 3 轮：流程效率 + 预测升级 + 健壮闭环）

**多智能体三轮迭代多智能体三轮迭代**：

**R1 流程效率**：
- **导入映射记忆**（`import_memory.py` 新增）：上次导入列映射自动预填预览对话框（normalize 后核心字段全命中才预填），确认后存回；设置页可查看摘要/一键清除
- **窗口状态记忆**：退出保存窗口 geometry，启动恢复（`clamp_geometry` 纯函数越界保护：负坐标/超屏回落默认）；设置页「恢复上次窗口位置」开关
- **批量图片 OCR**（`ocr.batch_ocr_images` 引擎 + 主页「批量图片」入口）：多选图片逐张进 TaskQueue 排队识别（状态栏「第 i/N 张」），单张失败不中断、收尾汇总失败原因，F9 可取消，与批量识别互斥
- 新增 `test_batch_ocr` 21 / `test_import_memory` 33 / `test_workflow_memory` 19 / `test_r1_baseline` 38 用例

**R2 预测升级**：
- **安全库存自动推荐**（`algorithm_ui.py`）：推荐 = ceil(1.65 × σ(近30天日销) × √运输天数)，数据不足不给推荐；计算后写缓存，设置页展示「上次推荐」+「一键应用」
- **预测日销**：指数平滑（SES, α=0.5）预测下一期日销，结果表新增「预测」列、导出同步；单品历史折线追加预测段
- 新增 `test_forecast` 37 / `test_forecast_gui` 用例

**R3 健壮闭环**：
- **备份/恢复/快照**（`backup_store.py` 新增）：设置+地区配置一键导出 zip（可含 history.db 一致性快照 VACUUM INTO）、从备份恢复（JSON 校验 + .pre_restore 原文件留存 + 路径穿越防护 + license/usage 缓存失效）、历史库单独快照；设置页「备份与恢复」卡
- **全局异常守卫**（`exception_guard.py` 新增，实现）：Tk 回调 + 子线程未捕获异常统一收口——完整 traceback 脱敏进日志 + 同类异常 10 分钟限流友好提示 + 钩子自身永不抛；启动检测 .pre_restore 残留
- **遗留修复**：恢复窗口位置开关生效、计算后刷新设置页推荐展示、备份恢复后 license 缓存显式失效
- 新增 `test_backup_store` 23 / `test_exception_guard` 20 用例

**审查闭环**：`docs/BUG_HUNT_FUTURE_R1.md` / `BUG_HUNT_FUTURE_R2.md` / `REVIEW_FUTURE_R3.md` 三轮报告落盘；R3 遗留 3 项（几何开关/推荐刷新/license 缓存）随 t10 一并修复；DESIGN.md §7 增补「导出列口径」拍板。

**测试总账**：**915 项全绿**（v1.5.4 的 707 + 新增 208：r1_enhanced 96→复用于轮审 + batch_ocr 21 + import_memory 33 + workflow_memory 19 + r1_baseline 38 + forecast 37 + backup_store 23 + exception_guard 20 + 其余微增）

**守约**：经典补货公式零改动；DESIGN §1/§2/§3/§4/§5 全部保持（columns=None 6 处、店仓隔离、失败显式、敏感文件零入仓）；git 全程单写者纪律，事故发生（队员误 reset/checkout 抹队友未提交改动 ×2）均已重做+禁令落管理。

## v1.5.4（自主迭代 3 轮：bug 审查 + 布局优化 + 回归强化）

**多智能体三轮迭代多智能体协作开发，36 条发现 → 修复闭环**：

**R1（审查 + 布局 + 测试强化）**：
- 全量 bug 审查（`docs/BUG_HUNT_R1.md`）：v1.5.3 新增代码 19 条发现 + 1 条宪法级（§6 版本规则，已修宪——见下）
- 布局优化批次A/B：设置页店铺管理/补货策略卡、历史页筛选条（`a98db0d`）；主页按钮排布、结果表新增「模型」列 + 列宽自适应 + 超长商品名省略、批量进度条 + 状态栏「⏳ 批量识别 N%｜阶段」、复核弹窗按钮归位（`a23f0c1`）
- 测试强化：`test_r1_enhanced.py` +96 用例（并发/损坏/极端输入/集成契约），总用例 538 → 634

**R2（修复 + 交叉复审 + 回归门禁）**：
- 修复 9 条非 GUI BUG（`bfb7087`）：复核白名单扩 region/warehouse、async_queue shutdown 置 cancelled、删死代码、算法回退标签语义、重名消歧等
- 修复 9 条 GUI BUG（`2bdcce1`）：**Critical×2**——主窗关闭时任务队列清理（in-flight API 半截中断）、切店重入竞态（跨店错位 0.5s）；High×2——`_calc_from_items` locals() 反模式、复核编辑放开 region/warehouse；Medium×5（历史页进店联动、弹窗关闭语义、F9 阶段5 干等、批量 blur 现场采集、导入路径复核触发）；布局批次C（复核按钮禁用态、切店器 busy 禁用、进度地区联动文案）
- 交叉复审（`docs/REVIEW_R2.md`）+ 回归门禁：707 测试全绿、13 处修复抽验 PASS

**R3（终审 + 发布）**：
- 终审 `docs/REVIEW_R3.md`：707 测试全绿 + py_compile + import 冒烟 + 宪法 §1-§5 核对通过；R1/R2 High/Medium **11/11 闭环**；遗留 9 条 Low 存档（L-2~L-9 均为 UI 历史遗留/产品决策项，不阻断发布）
- **修宪**：DESIGN.md §6 版本规则更新为 v1.5.x 线（商业化 v1.5.0 起已实际取代 v1.4 线，R3 L-1 裁决）

**测试总账**：`test_smoke` 240 + `test_r1_enhanced` 96 + `test_algorithm` 53 + `test_algorithm_ui` 63 + `test_review_flow` 61 + `test_store_db` 31 + `test_async_queue` 27 + `test_ocr_confidence` 39 + `test_store_ui_logic` 24 + `test_layout_logic` 29 + `test_home_layout` 44 = **707 项全绿**

**守约**：经典补货公式零改动；DESIGN §1/§2/§3/§4/§5 全部保持；gui.py 单写者纪律下无跨写冲突；敏感文件零入仓。

## v1.5.3（二期升级：多店铺隔离 + 高级补货模型 + 全局任务队列 + OCR 置信度复核）

**多智能体协作开发多智能体协作开发**，六项升级：

1. **多店铺隔离**（新增 `store_registry.py` / `store_ui_logic.py`）：主页地区行同排手动店铺切换器；设置页「店铺管理」卡片（新增/重命名/设为当前/删除，删除三选联动清历史）；每个店铺独立的 regions.json 设置、内存 cache、history.db 数据（`history_rows` 加 store 列，老库自动 ALTER 迁移、旧行归「默认店铺」，零丢数据）；历史趋势页按店铺筛选；导出结果加「店铺」列
2. **高级补货模型**（`utils.py`）：新增可选 `advanced` 模型——季节系数 + 大促权重（可配多组日期区间/权重）+ 滞销预警 + 超卖风险提示；经典公式一字未改，缺历史数据逐商品回退经典（标注 classic(no_history)/classic(error)），其余模式行为不变；设置页「补货策略」卡可选并配置因子；结果表/Excel 导出加「预警」列
3. **全局任务驱动架构**（新增 `async_queue.py`）：批量识别/实时截图/图片文件 OCR/表格导入四条后台路径统一走 TaskQueue（线程池 + 状态机 + 进度回调 + 协作式取消 + 异常隔离），UI 不再卡顿；F9 紧急停止语义保留（cancel_event 挂 `_batch_stop`）
4. **OCR 置信度校验**（`ocr.py` + 新增 `ocr_review.py`）：Laplacian 模糊检测 + 数字合理性校验 + 汇聚既有双模型 `_low_confidence` → confidence 元数据；计算前低置信行弹复核窗（全部接受 / 修正后重算 / 取消），模糊截图显式提示重截
5. **容错收口**：12 类常见异常（CSV 编码/Excel 损坏/行超限/API key 缺失/quota/超时/JSON 解析/无模型/模糊图等）统一用户可读中文报错（`_friendly_error` + `USER_MSG_*` 常量），不再裸露出错
6. **测试**：`test_smoke` 240 项保持无回归；新增 `test_store_db` 31 / `test_algorithm` 53 / `test_algorithm_ui` 63 / `test_ocr_confidence` 39 / `test_review_flow` 61 / `test_async_queue` 27 / `test_store_ui_logic` 24 —— 合计 **538 项全绿**

**守约**：DESIGN §1 全列识别（columns=None）、§2 模型分型、§3 店铺/地区数据独立性（切店重建 rows 防串）、§4 失败显式哲学、§5 敏感文件不入仓(git 基线 4107b97 起全程) 全部保持；经典补货公式逻辑零改动。

## v1.5.2（8/29 全量 bug 审查修复包）

**三人交叉审查34 条原始发现 → 逐条核实**：

**已修复（4 处，TestGating/TTL/回滚新用例 +6 → 203 全绿）**：
- `auth/license.py`：get_tier 缓存加 300s TTL（修「license 过期/外部改配置后 tier 判定陈旧至重启」——enforce=false 阶段休眠、收费启用日即爆发日）
- `settings_ui.py`：enforce 开关与 license 导入写盘失败时回滚 UI 状态（修「界面已翻转、实际没写进」的假状态）
- `gui.py`：加权模式异常回退标注 `classic(error)`（与「无历史」回退区分，导出 Excel 排障不再误导）；删除 `self._tier` 只写不读死状态

**审查后裁定不修（存档备查）**：另三条疑似问题经核实不成立——updater:1215 `'new_dir' in dir()` 恒真非死代码（new_dir 于 1129 行恒先绑定）；dpapi pbData 无悬挂（ctypes pointer/cast 的 _objects 链持活中间数组，且 CPython 引用计数下若真悬挂则测试不可能全绿）；镜像 URL 拼接属用户自有配置、威胁模型不成立。其余 P2/P3（os.replace 重试窗、progress 文件共享、memo 全清、GetLastError 时机、enc 类型强制、logger %-args 脱敏、定位 3 次采样成本等）裁定为：GIL 兜底/诊断质量/既有设计/产品决策，不构成本版本修复项。**「3 次定位采样=3 倍成本」与「指纹持久化」列入收费启用前决策清单。**

**收费启用前置清单（累计）**：① genkey 生成密钥对并回填 `auth/license.py _PUBKEY`；② 评估机器指纹持久化；③ 定位采样次数产品决策。

## v1.5.1（8/29 补强：补货模型框架 + 批量成本预估）

**P3-A 补货模型框架（经典保留 + 加权新增）**：
- `utils.py` 新增 `calc_replenishment(items, region, model, safety_days, in_transit_qty, shipping_lookup, history_lookup, offset=1)` 统一入口；
  `model='classic'` 原样保留 v1.4.7 公式（`补货时间=库存÷日销−(运输+offset)`、`补货量=日销×8`+100 取整）—— 一行逻辑都不改
- `model='weighted'` 新逻辑：日销 = 0.5×近7日 + 0.3×近14日 + 0.2×近30日（数据源 `history_db.query_sku_history`，sku_id 优先 / (region,name) 兜底关联）；
  qty = max(0, (运输天数+安全库存天数)×日销 − 在途 − 库存)，100 取整
- 缺历史数据的商品在**该商品级**回退经典公式并标注「经典(无历史)」，不影响其他行
- `settings.json` 新增 `replenishment.{model, safety_days:2, in_transit_qty:0}` 节点（模板自愈补全）
- `settings_ui.py` 新增「补货策略」UI 区块（模型单选 + safety_days spinbox + in_transit_qty spinbox）
- `gui.py _calc_from_items` 按配置分发模型，结果行携带 `model` 标注；列表与 Excel 导出列尾追加「模型」列（`export_xlsx.py` 同步，旧表不破坏）
- **失败哲学（DESIGN §4）**：加权模式任何异常（历史库损坏/字段缺失）逐商品回退经典公式并 log，绝不中断

**P3-B 批量前成本预估确认框**：
- 批量识别前弹 `messagebox.askyesno` 确认本次预计消耗（¥X.XX ~ ¥Y.YY 区间），防 API 成本突袭
- 价格表完整时按 input_per_million / output_per_million / image_per_call 算；
  缺价时显式 '?'（不内置默认价）；价格未配 0 估
- 模型选择、主+副模型都算入；`calls ≈ region_count × (1 + rounds × (1 定位 + 1 OCR × 模型数))` 保守估算

**测试锚点**：TestReplenishmentModels（12 用例）+ 独立复核（基线公式提取 + 8 组全新输入手算逐位比对 + 加权回退/有历史 2 项，）覆盖经典等价性 / 加权计算 / 无历史回退 / 异常回退

**守约**：经典模式输出与改动前完全一致（一行公式逻辑未改）；导出追加列缺省对旧表无副作用

## v1.5.0（8/29 商业化首版：授权框架 + 首页双入口 + Pro 门控默认全免）

**P2-A 纯 Python Ed25519 离线卡密授权（auth/ 模块）**：
- `auth/ed25519_verify.py`：纯 Python RFC 8032 Ed25519 verify 实现，零新依赖；TEST1+TEST2 真验签通过，篡改 msg/sig/bad PK 全部正确拒绝
- `auth/license.py`：机器指纹 = sha256(uuid + node + USERNAME)[:16]；license key = base64(ed25519_sign(machine_fingerprint + payload))
- 离线卡密：私钥签发 → 客户端离线 verify → 无网络依赖；机器指纹 + 过期时间绑死一台机

**P2-B 首页双入口布局（gui.py + 使用说明 + README）**：
- 主页按钮区重排：实时截图 + 📥 导入表格 在第一排并列（两者同 kind=dark 9pt bold，padx=12，几何对称）
- 第二排：🔄 刷新计算 + 📋 批量识别（次要功能） + 截图识别（弱样式）
- 不引入新配色/字体（仅复用现有 _mk_btn kind 与 FONT 字号）
- 实时截图/导入表格/刷新计算/批量识别/双模型/导出 Excel 全部在第一屏可见可点，不新增层级
- 文档同步：使用说明 §一 改写为「识别或导入」双入口；README 功能表 + 使用方法 + 表格导入章节同步

**P2-C Pro 门控接线·默认全免**：
- 全部功能默认免费（FREE_DAILY_LIVE_SCREENSHOT=50 / FREE_HISTORY_DAYS=30）；enforce=false 时不限制
- 卡片角标 FREE / PRO 仅在 enforce=true 且超限时显示
- 卡密 = ed25519_sign(machine_fingerprint + payload)，含 `unlock_daily_live` / `unlock_history_days` / `expires_at` 字段
- 用户没卡密时所有功能照常用（不强制 Pro），仅超过免费额度时弹 Pro 引导

**测试锚点**：auth.ed25519_verify RFC8032 TEST1+TEST2；license verify 真签/假签/篡改/过期四态；Pro 门控默认全免 4 档额度

## v1.4.8（8/29 保命：EULA + DPAPI + 镜像）

**P1-A EULA + 首启弹窗 + 密码不落盘（t7 + t16）**：
- `docs/EULA.md` + `eula_text.py`（PyInstaller 打包后无需读 docs/ 也能弹）：中文 7 条核心条款（账号风险自负 / 数据本地化 / 凭据保护 / 识别准确率免责 / 平台协议遵从 / 版本升级重确认 / 责任上限）
- `gui.py __init__` 在 `_build_ui` 之前强制 EULA 弹窗：未同意则 `sys.exit(0)`，不写任何 settings
- `_show_eula_dialog` 用 `dlg.update_idletasks() + dlg.grab_set() + wait_window()` 三段阻塞（**t16 修复 t7 闪退 bug**：原实现 grab_set 后未 wait_window 就返回，导致 _check_eula_accepted 二次校验 settings.json 还没写就 sys.exit）
- 「记住密码」默认关（t7 P1-A 修订）：勾选才落 DPAPI 加密密码；不勾选强制为空
- 历史密码一次性自动清空（已迁移用户）：首次启动 `meta.dpi_v=1` + 强制 `backend.password=''`

**P1-B 更新器国内镜像 + SHA256 安全校验（t8 + t17）**：
- `updater.py` 镜像链：github-kotori → github 直连 → 阿里云 OSS（`settings.update.mirror_oss` 可配） → 蓝奏云（`settings.update.mirror_lanzou` 可配）；任一源 HTTP 成功即返，校验失败换源
- `download_asset(... expected_sha256=None)` 新增形参：流内下载后立即就地校验，不匹配 → log + 删残文件 + continue 换下一源（**t17 修复 t8 偏差**：旧实现只 HTTP 成功即返、哈希失败发生在下游不换源）
- `_candidate_settings_paths()` 路径查找顺序：frozen 先 `%APPDATA%\PDD补货助手\settings.json` 再 exe 目录兜底；非 frozen 仅 `__file__ 目录`（**t17 修复 t8 偏差**：旧实现只查 exe 目录，打包版永远读不到 APPDATA 里的 settings）
- `main()` auto 模式：先下 .sha256 学期望 → 传期望进 `download_asset` → 下载+就地校验+不匹配换源一条龙
- `README.md` 新增「下载与安全校验」章节（国内镜像 / Get-FileHash 教程 / SmartScreen 放行 / 免责占位）

**P1-C DPAPI 凭据加密 + 日志脱敏（t9 + t18）**：
- `dpapi_utils.py`（新）：纯 stdlib + ctypes 调 Crypt32.dll CryptProtectData/CryptUnprotectData；
  当前用户作用域（dwFlags=0）；输出格式 `"dpapi:v1:<base64>"`；明文直通 + 损坏密文抛 DPAPIError
- `utils.Config._migrate_secrets()` 首启静默迁移：扫描 `api.providers.*.api_key` 与 `backend.password` 非空明文 → DPAPI 加密覆写 + `meta.dpi_v=1` 防重复；DPAPI 不可用时静默保留明文（沙盒环境仍可用）
- `utils.Config.decrypt_value()` + 模块级 `decrypt_secret()`：UI 端 / 运行时两套入口（前者返空串不抛、后者带 256 条 memo 缓存）
- 运行时 5 处接线（**t18 修复 t9 致命遗漏**：t9 只接了 UI，运行时直接拿密文发厂商必 401）：
  - `ocr.py:502` — 主 provider key
  - `ocr.py:590` — forced_model 走副 provider 切换
  - `ocr.py:648` — fallback 到智谱端点的 GLM key
  - `vision.py:199` — `_resolve` 嵌套函数每 provider key
  - `vision.py:271` — fallback 到智谱端点的 GLM key
- `utils._sanitize_for_log()` 日志脱敏：覆盖 `api_key` / `password` / `Authorization` / `Bearer` / `access_token` / `secret`；保留键名仅替换值为 `***`；`logger.py` Formatter + `ocr._ocr_dlog` + `ocr._write_ocr_debug` note 三处全部接入

**测试锚点**：TestDPAPI（9 项：is_encrypted / roundtrip / passthrough / corrupt / decrypt_value / migrate_secrets / noop / sanitize / real_log）+ TestKeyDecryptWiring（3 项：ocr/vision 源码断言 + decrypt_secret 行为 + Config.save 不是死代码回归）

**守约**：零第三方依赖（ctypes + stdlib only）；增量包链不破；PyInstaller spec 零改动

## v1.4.7（8/28 商业升级轮：数据累积 + 表格导入 + 用量可视化）

**WS-A 数据资产本地累积（history_db.py + 趋势 UI）**：
- 新增本地 SQLite 历史库（纯 stdlib，零新依赖）：每次识别/导入 = 一个 session，
  业务字段（name/stock/sales/sku_id/region/warehouse/days_left/status/qty）按次追加落库；
  WAL + busy_timeout + 模块级写锁，批量/实时线程极端并发不损坏
- 历史库失败安全铁律：任何异常仅记日志绝不中断识别；打开时 quick_check，
  损坏自动改名 history.db.corrupt 重建；双阈值保留策略（默认 180 天 / 20 万行，启动自动清理）
- GUI：地区 tab 行尾新增「📈 历史」——按日/地区汇总（商品数/预警数/库存合计），
  双击看当日明细，再双击看单商品库存趋势折线；「清空全部历史」二次确认；
  首次启用一次性隐私提示（数据仅本机持久化，不上传）
- 删除地区联动升级：三选「删除并清除历史 / 仅删配置保留历史 / 取消」

**WS-B 市场拓宽：CSV/XLSX 结构化导入（table_import.py + 导入 UI）**：
- 主页新增「📥 导入表格」：CSV（GBK/UTF-8-BOM/UTF-8 三级编码探测）与 XLSX 导入，
  复用 OCR 同款补货计算管线（parse_items_generic + 数字单位解析 + 公式注入清洗）
- 映射预览对话框：表头↔字段对位 + 缺失清单 + 可改下拉 + 「生成模板」；
  缺商品名/库存/销量关键列显式拒绝，不静默 fallback
- 行级导入报告（错误/警告计数 + 前 200 条明细）；1 万行上限；
  无销售区域列时归入当前地区并显式提示

**WS-C 成本可视化（usage_extractor.py + usage_store.py + 费用面板）**：
- OCR/视觉两漏斗返回三元组 (content, mdl, usage)，6 步降级链抽取 + 兜底估算，
  识别失败/缺 usage 不中断主流程；单点落账 usage_log.jsonl（估算行不计费、缺价不猜测）
- 工具条新增「本次 ¥X.XX｜本月 ¥Y.YY」实时费用显示；API 管理页新增「💰 用量明细」：
  今日/本周/本月/总计 4 档聚合 + 按模型/按用途分布 + 价格表编辑（元/百万 token，
  含每张图价，存本机配置）+「重置本月数据」二次确认
- 费用口径：估算仅供参考（'~' 前缀、不计费）；未配置价格显示 '?'（不内置默认价）

**配置与打包**：settings.json 新增 usage（采集开关/价格表/debug 归档开关，默认关）与
history（保留策略）节点，模板自愈补全；usage_log.jsonl / history.db 等运行时产物不入 git；
sqlite3 走 stdlib 无需改 spec

**v1.4.x 导航页重构**：📈 历史趋势 / 💰 用量明细 从 Toplevel 弹窗独立为导航页
（stats_ui.py 单一实现）；地区 tab 行尾「📈 历史」按钮改导航跳转（保留为快捷入口），
API 设置页底部「💰 用量明细」按钮整体移除；每次切入 after_idle 调度刷新（worker 不直调 Tk）

**测试锚点（19→115 项）**：并入 WS-B 导入 36 项（三编码读取/公式注入拦截/列名精确匹配/
万-千分位单位解析/模板生成）、WS-C 用量 52 项（6 形态降级链/缺价/零写盘开关/三元组契约/
漏斗单点落账恰一行）、WS-A 历史库 8 项（往返/并发写/损坏重建/只读故障注入/prune 双阈值）

**留待后续（P2）**：批量预算熔断（超出自动停止，配置键已预置）、真实多提供商 usage
形态实测归档（debug_archive_enabled 保留捕获能力）。本次版本费用均按配置价格表估算，
金额仅供参考。

## v1.4.6（8/23 P2 加固轮，采纳 dsh fix-review round2 各域报告）

**语义正确性**：
- 更新器 deleted-files.txt 删除时机移到覆盖成功之后（R1/C13）——覆盖中途失败（锁/权限/磁盘满）时旧模板/资源不再被提前删走，回归"更新失败但程序半损坏"中间态
- AI 读官方总条数超时 30s→180s 对齐其余视觉定位（F9 补漏，弱网大表不再误报超时）

**GUI 健壮性**：
- 批量识别双批并发守卫（F24 收尾）——`_batch_running` 运行中标志，批量进行中禁止再开批量对话框/重入，防双批互相覆盖取消钩子与物理争抢；异常路径与收尾清标志
- 运输时效设置输入收紧（L8/L14）——spinbox 任意文本/小数/越界值 clamp 到 1..30 整数，批量全设同收紧
- "立即定位"连点重入守卫（C11 尾项）——定位进行中禁止重复触发，防 last-write-wins

**防御纵深 / 数据一致性**：
- 更新器覆盖回滚扩展捕获 OSError（F22）——磁盘满等错误不再只认 PermissionError，主 exe 覆盖失败同样回滚保全
- GUI 下载终检补强（F13 纵深）——sha 文件值强制 64 位 hex 格式 + 声明大小 vs 实收字节数终检，双保险拒截断/篡改包
- Config.load 返回深拷贝（F20）——调用方原地修改不再污染 mtime 缓存，避免跨轮读脏数据
- 损坏 settings.json 自动处置（C12）——改名 .corrupt 防反复读损坏 + 从 .bak 恢复上次好配置（仍保留原文件供人工抢救）
- 打包资源 visited 去重（L21/R4）——同资源 onedir/_internal 双存在不再生成 zip 重复条目

**测试锚点（15→19 项）**：补 deleted-files 删除生效/白名单拦截、copy2 OSError 回滚、版本比较语义、损坏配置恢复断言

**已知残留（非阻断）**：F4 非名称列尾数字剥离依赖列语义参数，留待后续；L1/L2/L4/L5 等低危项、M1-M6 工程债未动

## v1.4.5（8/23 全量代码审计修复轮，采纳 dsh bug hunt 报告）

**高危（F 系列）**：
- 熔断标志跨批量复位（F1 补充）+ export_path 空串回退桌面 + strip_region_suffix/import os 两个 NameError 修复（多省时效混算、AI 定位入口不可用）
- 导出误伤修正：strip_tail_noise 整值剥空保留原文、名称列不再剥尾数字（F4）
- 旧 API 配置迁移死代码：判定基于原始文件，旧用户 key 升级不再丢失（F7）
- _suspect_number 不再覆盖真实库存/销量（F8）；官方总数正则优先"共有N条"（F11）
- 损坏 settings.json 不再被模板静默覆盖（F21）；单位提取先去词条噪音（F25）
- 视觉定位链读取超时 30→180s 对齐（F9）；anomaly 布尔串显式判定（F10）；VisionCancelled 透传（F26）
- 更新器链：_is_file_locked 句柄判定修复、覆盖事务化回滚、SHA256 fail-closed、_is_program_dir 收紧、
  deleted-files.txt 旧文件清理、updater 依赖与资源清单补齐、自动生成 .sha256、git 缺失报错、auto 版本比较（F5/6/13/14/15/16/17/18/19/22）
- GDI 释放顺序规范（F12）；Config 保存加锁+tmp 唯一（F20）；Tk 主线程契约/批量入口禁用/F9 清点提前/
  AI 定位与公告图线程化（F23/24/27/28/29）

**低危（L 系列核心项）**：补 F4/F11 回归单测、废弃行切分标注、注释同步、死逻辑清理

## v1.4.4（8/22 代码审查修复轮，采纳 dsh 评估报告 #1/#5/#9）

- **熔断标志跨批量复位（报告 #1）**：`_api_fatal` 置位后此前无任何清零点——一次批量额度耗尽会让同一进程后续所有批量永久熔断（换 key 也不恢复）；改为批量启动时清零，批内熔断语义保留，新一批从头可用
- **regions.json 原子写（报告 #5）**：旧实现直接 `open('w')` 覆盖，崩溃/断电可能损坏地区时效配置；改为临时文件 + `os.replace` 原子替换，与 Config 同一模式
- **文档对齐实现（报告 #9）**：README/使用说明 的 Excel 描述改为"每次导出建一个带时间戳的 Sheet，地区作为行内首列"（此前误写"按地区/日期分 Sheet"，与实际 export_xlsx 实现不符）

## v1.4.3（8/20 客户实测修复轮次）

- **官方总数权威化（客户要求：直接抓官方数据对比判结束，别靠重试撞）**：右下角分页栏"共有N条"特写识别（整屏压缩后小字糊、页面5个读成3的根因）；首轮总是用特写读官方 N 覆盖 AI 定位的不可靠总数；滚动决策新增权威硬停——累计识别量 ≥ 官方 N 立即"识别齐全，结束滚动"
- **VL 大图读取超时修复**（客户质疑"你给 vl 留了多久处理图片"）：读取超时 30s→180s，9 列大表不再必超时误判；错误分诊（模型处理慢/网络不通/限流分开口径）+ 日志 120 字符
- **大表(9列)三修**：滚动轮网络容错（重试2次+断网不计入无数据）；副模型 qwen*-ocr 不参与表格JSON验证（输出文字块列表非表格JSON，直接单模型）；全列输出预算 2048→4096（按模型档钳制）
- **JSON 解析健壮性**：输出上限按模型分档 `_pick_max_tok`（弱模型不 400 + 超限自动砍半重发）；截断容错 `_recover_partial_json`（捞回完整行不再整轮归零）；失败现场落盘 `_write_ocr_fail` 可溯源；报错带摘要
- **幻觉过滤误杀修复**：总数可信度校准 + 全删保护（页面5个、AI总数误读3、真实5行被"5→0"全删死循环；改为保底保留首轮全量）
- **批量两修**：防跨省串数据（查询未生效→显式跳过，不再放行识别上一省数据）；滚动力度累计到 -300×2
- **紧急终止立刻生效**：ocr/vision 请求层取消钩子，F9 后下一个 API 请求点即中断（不再等 60~90s）
- **滚动落点大修**：滚动前激活浏览器前台（根治"鼠标放任务栏/跑其他页面"）+ 落点钳制避 HUD/任务栏 + 滚动后中部横带检测

## v1.4.2（8/15 数字精准度优化轮次）

- **图像预处理增强（参考手机端图文识别链路）**：自适应对比度 + 文字边缘锐化 + JPEG 质量 80→95 + 批量降采样 1280→1920（保留小字细节）——修复批量识别"1234→123"数字丢位（降采样+有损压缩导致末位丢失）；行切分组图同步增强
- **数字完整性 prompt 强化**：库存/销量末位 0 必须保留、禁止丢位/省略
- **滚动轮识别统一收益**：滚动加载每轮走增强预处理，数字稳定后 dedup 三路判定更可靠
- **更新器修复轮**（v1.4.1 已记）：finalize 剥目录/sha 镜像/关窗取消/按钮标版本

## v1.4.1（8/12 修复轮次）

- **行级切分可靠性修复链**：①全列 token 截断（数量莫名变 2）——分组按模型输出上限自适应（glm 2 行/qwen-omni 4 行/OCR 6 行）+ JSON 截断自动拆半重试；②AI 行边界漏行（3 行小表格只识别 2 个）——gui 总数校验回退整表 + row_split 完整性校验 + bbox 不截行；③单行孤组列不全（山东第 4 行）——分组避免孤行；④表头判断修正（不误回退整表）+ 校验阈值放宽
- **客户区偏移修复**：PrintWindow 截客户区但偏移用外框坐标 → 非最大化窗口点击偏左上（客户反馈查询按钮偏左）；改 ClientToScreen 取客户区起点；查询点击后页面无变化自动重定位重试
- **多省份显示隔离**：批量结果按地区分组，表格只显示第一个地区，其余走地区 tab
- **首轮定位 AI 优先**（模板匹配降级兜底，防误匹配销售区域框）；校准模块让位（隐藏窗口露出浏览器）
- **设计标准固化**：docs/DESIGN.md（全列识别+程序端筛选/模型分型/多省份独立/失败哲学/安全底线/回归记录/审查清单）

## v1.4

### 🔧 8/6 稳定性大修（全链路审查 + 模型选型重构）
- **批量识别数据修复**：多省份不再互相污染——填充期间抑制排队重算（原 N 行排队 N 次全量重算覆盖最后省份缓存）、切地区同步重建 rows、每商品用自己的 region 查时效
- **滚动去重增强**：dedup 三路判断（ID精确/ID近匹配+name前缀/name强相似兜底），sku 整段错位不再重复输出；同名不同规格保留
- **动态列渲染修复**：表头按勾选列动态生成，数据逐列对应不再串位；双击编辑按列名反查字段（修复弹框取错列）
- **回归设计初衷——全列识别 + 程序端筛选**：模型识别整张表所有列，勾选列/筛选是程序端行为；列名模糊匹配（≤2字差异+前3字相同+name永不模糊）容忍模型抄错列名
- **模型选型规则**：OCR 专用模型（qwen3.5-ocr/qwen-vl-ocr 系列）只做文字提取；定位/状态机/异常检测用通用视觉；主模型 OCR 型时用副模型，都不可用明确报错不静默回退；副模型支持跨 provider（主 glm + 副 qwen-ocr 正常）
- **截图识别双弹修复**：_click 防重入三层防护（时间戳 + 执行中标志 + 延迟复位），模态对话框关闭不再二次弹框
- **实时截图窗口恢复**：主线程 2 秒无条件恢复 + 置顶，最小化最多 2 秒
- **仓库信息清洗**：warehouse 用已过滤字段（不再显示"查看地址"），stock/sales 列渲染过 strip_tail_noise（去"08-06 21:30 更新记录"噪音）
- **UI 修复**：首页模型徽章 FREE/PRO 可读（终末地主题对调配色）、副模型下拉框动态同步供应商新模型、商品信息列宽 260
- **Qwen OCR 模型适配**：识别 qwen*-ocr 系列时 max_side 提到 2560 保留小字、max_tok 吃满 4096

### 🧠 借鉴开源方案的三项升级
- **表格行级切分识别**（借鉴 Surya 行级 bbox）：AI 定位表格时返回每行边界，按行分组切图独立识别——模型只能抄当前组内容，从根源上压住豆包乱编
- **验证码/异常弹窗检测**（借鉴 Granblue）：省份切换失败时 AI 判断是否验证码/模态弹窗，是则明确提示人工处理，不再盲目重试
- **页面状态机**（借鉴 Granblue）：每省份开始前识别页面状态（正常/登录过期/验证码/弹窗/空白），登录过期中止批量，异常弹窗跳过省份

### 📦 构建与更新
- 打包恢复 onedir 结构，增量更新机制恢复（老客户升级只下载 ~18MB）
- 更新器瘦身 68.9MB → 8MB（去掉误引用的视觉依赖链）

## v1.3

### 🎯 定位全面升级（不再依赖窗口最小化）
- 定位改用窗口截图：自动锁定商家后台窗口截图 + 偏移转全屏坐标，不依赖最小化
- 手动定位加切前台提示（3 秒倒计时切商家后台到前台）
- 批量识别每次启动实时 AI 定位（去掉 5 分钟静默缓存）
- 修复豆包 404：视觉定位优先用 custom_endpoint（ep-xxx 推理接入点 ID）
- 省份切换后 AI 验证筛选栏省份是否切成功；失败自动重新 AI 定位重选，仍失败才跳过

### 🛡️ 防假数据
- 无商品 ID 的行直接过滤（实测真实行 100% 带 ID，防豆包乱编名字混入）
- 每个省份开始前读页面商品总条数，滚动结束后与实际识别量对比提示不一致
- 滚动循环加内容指纹停止：连续 3 轮 stock 集合无变化即结束（防无限滚动）

### 🐛 稳定性修复
- 刷新计算后仓库等勾选列空白：修复 strip_tail_noise 误杀纯数字值 + 保留单位
- 主副模型相同警告 NameError、批量线程异常按钮恢复、API 空返回防崩溃
- 地区时效旧格式迁移保留默认天数（不再丢客户配置）

### 📊 业务优化
- 销量为 0 的商品标记「无销量·观察」，不再强制补货

## v1.2

### 🔍 OCR 识别精度大幅提升
- 自适应表格检测：自动定位表格区域，不再依赖固定裁剪
- 提示词重构 + 示例引导：模型"抄写"原文，数字解析交给代码
- 数字解析支持千分位、单位（万/千/w/k）、全角、约/共 前缀
- 列对齐校验：检测并修正 stock/sales 错位
- 双模型交叉验证（可选开关）：主+副模型比对，低置信度商品 ⚠ 标红
- 幻觉过滤器优化：不再误杀 2 字商品名、纯英文 SKU、同系列商品

### 🖥️ 多分辨率适配
- 模板匹配 7 档尺度（0.5x~1.5x），支持 4K/带鱼屏
- 点击偏移按分辨率自动缩放

### 🛡️ 稳定性与安全
- 修复导出 Excel 崩溃、批量识别线程崩溃、更新器自升级失效等 60+ 问题
- Excel 公式注入防护、更新包安全校验（SHA256/路径防护/解压上限）
- 补货偏移量可配置（settings 中 `replenishment_offset`）
- 批量识别失败不再静默，全失败有明确提示

### 🎨 体验优化
- 主题切换即时生效（已访问页面同步刷新）
- 分辨率预设恢复 UI 入口（通用设置页）
- 批量识别操作自动重试、临时文件自动清理
- 港澳台地区批量识别支持
