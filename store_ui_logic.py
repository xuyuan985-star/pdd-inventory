"""
多店铺隔离——UI 无关纯逻辑（t6）
================================

把 gui.py 的「切店铺 → 全量重建状态」与「按店铺组装历史入库入参」抽成纯函数，
供 test_store_ui_logic.py 无 Tk 单测（DESIGN §3 纪律的机器可验证形态）。
gui.py / stats_ui.py 是这些函数的唯一调用方；本模块不 import tkinter。

设计基线（DESIGN 宪法）：
- 切店铺与切地区同一纪律（§3）：目标店铺状态必须【全新构建】，禁止上一店铺
  cache/rows/plans 残留进新店铺（跨店污染 = bug）；
- 同店重复切换必须幂等（不重复重建、状态零变化）；
- 批量识别运行中禁止切店（互斥）：半程换店会让批量结果与历史入库店铺错位；
- record_capture 组装走双层 {store_id: {region: [plans]}}（t1 history_db 契约）。
"""

__all__ = [
    'DEFAULT_STORE_ID', 'resolve_store_switch', 'fresh_gui_state',
    'group_plans_by_store', 'store_choices',
]

DEFAULT_STORE_ID = 'default'
"""默认店铺固定 id（与 store_registry / history_db 同值）。"""


def resolve_store_switch(cur_store_id, target_store_id, stores, busy=False):
    """切店决策（纯函数）：返回 {'ok': bool, 'store_id': str, 'reason': str}。

    - 'ok-switched'      ：合法跨店切换（store_id = 目标店）；
    - 'ok-idempotent'    ：目标 == 当前店（幂等，调用方零重建；即使 busy 也放行——
                           重复选当前店不产生任何状态变化，无互斥必要）；
    - 'rejected-busy'    ：批量识别运行中禁止跨店切换（互斥）；
    - 'rejected-invalid' ：目标店铺不在 stores 清单（互斥）；
    - 'rejected-empty'   ：目标为空。

    stores：store_registry.get_stores() 形状 [{'id','name'},...]；脏条目容忍。
    当前店无法解析时按 default 处理（与 store_registry/history_db 同店语义）。
    """
    cur = str(cur_store_id or '').strip() or DEFAULT_STORE_ID
    tgt = str(target_store_id or '').strip()
    if not tgt:
        return {'ok': False, 'store_id': cur, 'reason': 'rejected-empty'}
    if tgt == cur:
        return {'ok': True, 'store_id': cur, 'reason': 'ok-idempotent'}
    if busy:
        return {'ok': False, 'store_id': cur, 'reason': 'rejected-busy'}
    ids = set()
    for s in stores or []:
        if isinstance(s, dict):
            sid = str(s.get('id') or '').strip()
            if sid:
                ids.add(sid)
    if tgt not in ids:
        return {'ok': False, 'store_id': cur, 'reason': 'rejected-invalid'}
    return {'ok': True, 'store_id': tgt, 'reason': 'ok-switched'}


def fresh_gui_state(store_id, regions=None):
    """切店后的全新 GUI 状态（纯函数，DESIGN §3「全量重建」的规范形状）。

    返回 dict：store_id / regions（目标店时效配置）/ cache（空）/ active_region
    （None）/ region_var（复位'未识别'）/ plans（空）。调用方（gui._apply_store_switch）
    把这些字段整体赋回 App 状态——禁止在旧状态上原地改（残留即跨店污染）。
    regions 非 dict（注册表读失败等）按空配置处理，绝不把上一店铺的数据带进来。
    """
    sid = str(store_id or '').strip() or DEFAULT_STORE_ID
    reg = regions if isinstance(regions, dict) else {}
    return {
        'store_id': sid,
        'regions': dict(reg),
        'cache': {},
        'active_region': None,
        'region_var': '未识别',
        'plans': [],
    }


def group_plans_by_store(plans_by_region, store_id):
    """把单层 {region: [plans]} 组装成 record_capture 双层入参 {store_id: {...}}。

    - store_id 空/None → 'default'（与 history_db 同店语义）；
    - 非 dict / 空 dict / 全空 plans → 返回 {}（调用方按空跳过）；
    - 外层与内层 dict 均新建（plans 列表浅拷贝一层；record_capture 只读 plans），
      防调用方后续改写污染入库形状。
    """
    if not isinstance(plans_by_region, dict) or not plans_by_region:
        return {}
    sid = str(store_id or '').strip() or DEFAULT_STORE_ID
    inner = {}
    for region, plans in plans_by_region.items():
        if isinstance(plans, (list, tuple)) and plans:
            inner[region] = list(plans)
    if not inner:
        return {}
    return {sid: inner}


def store_choices(stores, all_label=None):
    """店铺下拉选项助手（纯函数）：返回 (labels, name2id)。

    - all_label 非 None（如 '全部店铺'）→ 作为首项，映射 id=None（=查全部店铺）；
    - 重名店铺自动消歧：第二个重名项 label 追加完整 sid，保证 name→id 唯一
      （id 权威；label 只用于显示与反查）；原用
      sid[:8] 极端碰撞下仍撞名则丢，改为完整 sid + 序号兜底，绝不丢店；
    - 脏条目（非 dict / 缺 id）跳过。
    """
    labels, name2id = [], {}
    if all_label is not None:
        labels.append(all_label)
        name2id[all_label] = None
    for s in stores or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get('id') or '').strip()
        if not sid:
            continue
        nm = str(s.get('name') or '').strip() or sid
        if nm in name2id:
            # R2 问题 修复：用完整 sid 消歧，理论碰撞概率
            # 极低（UUID v4 128 bit）；若仍撞（实际不可能）则追加循环序号
            # 兜底，绝不丢店——原代码「continue 丢弃」是数据丢失。
            candidate = f"{nm}({sid})"
            i = 2
            while candidate in name2id:
                candidate = f"{nm}({sid}#{i})"
                i += 1
            nm = candidate
        labels.append(nm)
        name2id[nm] = sid
    return labels, name2id
