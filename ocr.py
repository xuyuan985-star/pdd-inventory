"""
PDD 后台截图 OCR 识别
输入：PDD订货管理页面截图
输出：[{name, stock, sales}, ...]
"""

import base64, json, os

import requests
from utils import get_api_config, get_base_dir


# ── 批量紧急停止钩子（v1.4.2）：紧急终止必须"立刻"——光靠调用方 Event 轮询，
# 要等当前 30~90s 的 OCR 请求跑完才轮到检查点。这里提供模块级取消检查：
# gui 批量线程注入 set_cancel_check(stop.is_set)，API 请求前/重试间立即中断，
# 抛出 BatchCancelled 让批量线程马上收尾，不再等超时。──
_CANCEL_CHECK = None


class BatchCancelled(RuntimeError):
    """批量识别被 F9 紧急终止"""


def set_cancel_check(fn):
    """设置/清除取消检查回调：fn() 返回 True 表示需要取消；传 None 清除。"""
    global _CANCEL_CHECK
    _CANCEL_CHECK = fn


def _check_cancel():
    """API 请求前调用：取消已触发则抛 BatchCancelled，立即中断当前请求链。"""
    fn = _CANCEL_CHECK
    if fn is not None:
        try:
            if fn():
                raise BatchCancelled("紧急停止（F9）")
        except BatchCancelled:
            raise
        except Exception:
            pass  # 检查函数自身异常不阻断识别


def _crop_bbox(image_path: str, bbox: dict):
    """
    按像素 bbox {left, top, right, bottom} 裁剪，返回 PIL Image。
    bbox 缺失关键键或非法时返回 None（调用方回退原图）。
    """
    try:
        from PIL import Image as PILImg
        if not isinstance(bbox, dict) or not all(k in bbox for k in ('left', 'top', 'right', 'bottom')):
            return None
        img = PILImg.open(image_path)
        w, h = img.size
        left = max(0, min(int(bbox.get('left', 0)), w - 1))
        top = max(0, min(int(bbox.get('top', 0)), h - 1))
        right = max(left + 1, min(int(bbox.get('right', w)), w))
        bottom = max(top + 1, min(int(bbox.get('bottom', h)), h))
        if right - left < 10 or bottom - top < 10:
            return None
        return img.crop((left, top, right, bottom))
    except Exception:
        return None


def _is_qwen_ocr(mdl) -> bool:
    """判断是否为 Qwen OCR 专用模型（阿里百炼 Qwen-OCR 系列）。
    这些模型专为文字提取优化：max_tokens 默认 4096（可到 8192）、支持大图保留小字细节。
    列表覆盖 qwen3.5-ocr / qwen-vl-ocr / qwen-vl-ocr-latest / qwen-vl-ocr-2025-11-20 /
    qwen-vl-ocr-2025-08-28 / qwen-vl-ocr-2025-04-13 / qwen-vl-ocr-1028 等。
    """
    if not mdl:
        return False
    s = str(mdl).strip().lower()
    if s.startswith('qwen3.5-ocr') or s == 'qwen3.5-ocr':
        return True
    if s.startswith('qwen-vl-ocr') or s == 'qwen-vl-ocr':
        return True
    return False


def _pick_max_tok(mdl, desired: int) -> int:
    """按模型能力对输出 token 上限分档（v1.4.2）——不能一刀切 4096：
    glm-4v-flash 硬上限 1024（超限直接 400）；qwen-omni 系常见 2048~4096；
    OCR 系/Qwen 大预算系到 8192；未知模型保守 2048（期望越大越容易 400）。
    返回 min(desired, 档位上限)，弱模型不会因上层传大数而 400。"""
    m = str(mdl or '').strip().lower()
    if m.startswith('glm-4v-flash') or m == 'glm-4v-flash':
        return min(desired, 1024)
    if _is_qwen_ocr(m):
        return min(desired, 8192)
    if m.startswith(('qwen', 'qwen3', 'qwen-vl', 'qwen-omni')):  # qwen 系多模态
        return min(desired, 4096)
    if m.startswith(('doubao', 'ep-')):
        return min(desired, 8192)
    if m.startswith('glm'):
        return min(desired, 4096)
    return min(desired, 2048)  # 未知模型保守 2048


def _recover_partial_json(text: str):
    """从半截/带杂质的 JSON 文本中尽量恢复出完整行（v1.4.2）：
    网络截断/模型输出夹杂说明文字时 json.loads 必然失败，直接抛错 = 整轮识别归零。
    这里按行边界/补括号策略逐步尝试，把能解析出的完整行捞回来。
    返回 (可用行数, columns, rows)；彻底失败返回 None。"""
    if not text:
        return None
    text = text.strip()
    start = text.find('{')
    if start < 0:
        start = text.find('[')
        if start < 0:
            return None

    def _ok(data):
        if isinstance(data, dict):
            if 'rows' in data:
                rows = data.get('rows') or data.get('items') or []
                cols = data.get('columns') or []
            elif 'columns' not in data:
                # 无 rows/columns 的普通 dict = 单独一行（行对象本身）
                rows, cols = [data], list(data.keys())
            else:
                return None  # 仅有 columns 无 rows = 半截结构，不作为有效结果
        elif isinstance(data, list):
            rows, cols = data, (list(data[0].keys()) if data and isinstance(data[0], dict) else [])
        else:
            return None
        if rows and isinstance(rows[0], dict):
            return (len(rows), list(cols), list(rows))
        return None

    # 0) 直接解析 start 之后的完整文本（处理完整 JSON / 前缀文字+完整 JSON）
    try:
        r = _ok(json.loads(text[start:]))
        if r:
            return r
    except Exception:
        pass
    # 1) 补右括号（截断常只差尾括号）
    for closer in ('}', ']', '}]', ']}'):
        try:
            r = _ok(json.loads(text[start:] + closer))
            if r:
                return r
        except Exception:
            pass
    # 2) 按行边界截取（截断常发生在行中间，上一行结构完整）
    for tok in ('}\n', '},\n', '},'):
        idx = text.rfind(tok, start)
        if idx > start:
            seg = text[start:idx + 1].rstrip()
            if seg.endswith('}'):
                try:
                    r = _ok(json.loads(seg))
                    if r:
                        return r
                except Exception:
                    pass
    # 3) 数组形式：截到最后一个完整 '},' 后补闭合（先 ] 关 rows 数组，再 } 关整体）
    idx = text.rfind('},', start)
    if idx <= start:
        idx = text.rfind('}', start)
    if idx > start:
        for ext in (']}', '}]', ']', '}'):
            try:
                r = _ok(json.loads(text[start:idx + 1] + ext))
                if r:
                    return r
            except Exception:
                pass
    return None


def _write_ocr_fail(content: str, err: str):
    """JSON 解析失败时的现场落盘（v1.4.2 诊断补盲）：
    _ocr_debug.json 之前只记成功记录，失败时刻完全不可见 → 无法定位截断点。
    现在把失败原文（前 800 字）+ 错误信息写入同文件的 fail 记录，可追溯。"""
    try:
        import time as _tv
        _d = os.path.join(get_base_dir(), 'output')
        os.makedirs(_d, exist_ok=True)
        _p = os.path.join(_d, '_ocr_debug.json')
        try:
            with open(_p, 'r', encoding='utf-8') as _f:
                _hist = json.load(_f)
            if not isinstance(_hist, list):
                _hist = [_hist]
        except Exception:
            _hist = []
        _hist.append({'ts': _tv.strftime('%H:%M:%S'), 'fail': True,
                      'err': str(err)[:200], 'content_head': (content or '')[:800]})
        with open(_p, 'w', encoding='utf-8') as _f:
            json.dump(_hist[-40:], _f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _prep_image_b64(image_path: str, max_side: int = 1920, quality: int = 95,
                    table_bbox: dict = None, enhance: bool = True) -> str:
    """
    统一图片预处理：画面增强（自适应对比度+锐化）→ 长边缩放 → 高质量 JPEG → base64。
    裁剪优先级：AI 定位 bbox（table_bbox）→ OpenCV auto_crop_table → 原图。
    所有提供商/模型共用，避免 doubao 压 1280 而 qwen/glm 直传原图的不一致。
    v1.4.2 对齐手机端图文识别链路：①自适应对比度（PDD 浅底浅灰字，低对比度
    让数字判读丢位）②文字边缘锐化（JPEG 有损糊掉数字末位 → 1234→123）
    ③JPEG q80→95 + 降采样 1280→1920（保留小字高频细节，代价是上传体积增大，
    对识别精准度收益远大于带宽成本）。
    """
    try:
        from PIL import Image as PILImg, ImageOps as _ImageOps, ImageFilter as _ImageFilter
        import io as _io
        # 自适应表格裁剪：优先外部传入的 AI bbox，其次 OpenCV 检测，失败回退原图
        cropped = None
        if isinstance(table_bbox, dict):
            cropped = _crop_bbox(image_path, table_bbox)
        if cropped is None:
            cropped = auto_crop_table(image_path)
        _img = cropped if cropped is not None else PILImg.open(image_path)
        if _img.mode != 'RGB':
            _img = _img.convert('RGB')
        # v1.4.2 画面增强：先增强再缩放（放大后增强会放大噪点）
        if enhance:
            try:
                _img = _ImageOps.autocontrast(_img, cutoff=1)
                _img = _img.filter(_ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=2))
            except Exception:
                pass  # 增强失败不影响识别（保持原图）
        _w, _h = _img.size
        _r = max_side / max(_w, _h)
        if _r < 1:
            _img = _img.resize((int(_w * _r), int(_h * _r)), PILImg.LANCZOS)
        _buf = _io.BytesIO()
        _img.save(_buf, format='JPEG', quality=quality)
        return base64.b64encode(_buf.getvalue()).decode()
    except Exception as e:
        # 预处理失败则回退原图（记录警告便于排查识别率下降）
        # ⚠️ 必须限制大小：PIL 损坏/模式异常时直接 base64 原图会把 20MB PNG
        # 原样上传，API 超限/费用激增（v1.4 审查修复）——回退也用 PIL 压缩，
        # 压缩再失败才返回 None 让调用方报错（不静默传大图）
        print(f"[OCR] 图片预处理失败，回退压缩图: {e} ({image_path})")
        try:
            from PIL import Image as PILImg
            import io as _io2
            _fb = PILImg.open(image_path)
            if _fb.mode != 'RGB':
                _fb = _fb.convert('RGB')
            _fb.thumbnail((max_side, max_side), PILImg.LANCZOS)
            _buf2 = _io2.BytesIO()
            _fb.save(_buf2, format='JPEG', quality=quality)
            return base64.b64encode(_buf2.getvalue()).decode()
        except Exception as e2:
            # 连 PIL 都打不开：明确失败，不静默传原图（调用方会抛错）
            raise RuntimeError(
                f"图片预处理与压缩均失败，拒绝上传原图: {e2} ({image_path})"
            ) from e2




# 全角→半角映射常量（模块级缓存，避免 _parse_num_text 每次调用重建）
_FULLWIDTH_TRANS = str.maketrans('０１２３４５６７８９．', '0123456789.')


def auto_crop_table(image_path: str):
    """
    自适应表格检测：用 OpenCV 检测 PDD 订货表格区域。
    返回裁剪后的 PIL Image；检测失败或无 cv2 时返回 None（调用方回退原图）。
    原理：表格行分隔线是横向贯穿线，二值化+形态学后行投影最密集的连续区域即表格主体。
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image as PILImg
    except ImportError:
        return None

    try:
        img = cv2.imread(image_path)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape

        # 二值化：反色让表格线变白
        _, binary = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)

        # 横向贯穿线检测（表格行分隔线）
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, w // 25), 1))
        h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)

        # 行投影：统计每行的横线像素数
        h_proj = np.sum(h_lines > 0, axis=1)
        line_threshold = max(10, w // 8)  # 横线至少覆盖 1/8 宽度才算分隔线
        line_rows = np.where(h_proj > line_threshold)[0]
        if len(line_rows) < 3:
            return None  # 横线太少，不是表格

        # 聚类：相邻分隔线差距 <= 60px 视为同一表格
        groups = []
        cur = [line_rows[0]]
        for r in line_rows[1:]:
            if r - cur[-1] <= 60:
                cur.append(r)
            else:
                groups.append(cur)
                cur = [r]
        groups.append(cur)
        # 取最长的连续分隔线组（表格主体通常分隔线最多）
        best = max(groups, key=len)
        if len(best) < 3:
            return None
        y_top = max(0, int(best[0]) - 60)  # 往上多留空间，确保表头行不被裁掉（表头在第一条分隔线上方）
        # 底部预留：最后一条分隔线通常是【最后一行数据的上边界】，不是表格底边——
        # 只 +10 会裁掉最后一行数据（v1.4 审查修复）。按平均行距向下扩展，
        # 若下方不足则直接取图底（不截断）。
        _avg_row = (best[-1] - best[0]) / max(1, len(best) - 1) if len(best) > 1 else 60
        _margin_bottom = max(60, int(_avg_row * 1.2), h // 20)
        y_bottom = min(h, int(best[-1]) + _margin_bottom)

        # x 范围：取该区域内横线的水平覆盖范围（贯穿线的端点即表格左右边界）
        region = h_lines[y_top:y_bottom, :]
        cols = np.where(np.sum(region > 0, axis=0) > 0)[0]
        if len(cols) < w // 4:
            return None
        x_left = max(0, int(cols[0]) - 20)
        x_right = min(w, int(cols[-1]) + 20)

        # 区域过小（< 原图 1/4 高度）视为误检
        if (y_bottom - y_top) < h // 4 or (x_right - x_left) < w // 4:
            return None

        pil = PILImg.open(image_path)
        return pil.crop((x_left, y_top, x_right, y_bottom))
    except Exception:
        return None


# v1.4.2 致命 API 错误熔断：额度耗尽/鉴权失败时重试无意义，标记后批量中止
# （避免每个省份白跑一遍；客户日志根因 = qwen Free quota exhausted）
_api_fatal = {'flag': False}


def _is_fatal_api_err(ex) -> bool:
    """致命 API 错误判定：额度耗尽 / 鉴权失败（重试无意义，应熔断并提示用户）。"""
    s = str(ex or '').lower()
    return any(k in s for k in (
        'free quota exhausted', 'insufficient_quota', 'insufficient balance',
        '余额不足', 'quota has been exhausted', 'quota exceeded',
        'invalid_api_key', 'unauthorized', 'authenticationfailed',
        'incorrect api key', 'authentication error', '403'))


def _mark_api_fatal(ex):
    """API 异常时若致命（额度/鉴权），置熔断标志（vision/gui 调用）。"""
    try:
        if _is_fatal_api_err(ex):
            _api_fatal['flag'] = True
    except Exception:
        pass


def _suspect_number(v) -> bool:
    """数字可疑检测（v1.4.2 温度稳定性）：年份模式（1900-2100 四位数）或
    含日期分隔符（2026-08-04）→ 疑似列串位（行切分小图把"商品创建时间"
    等日期列抄进 stock/sales），触发二次识别择优补全。"""
    s = str(v or '').strip()
    if not s:
        return False
    digits = ''.join(ch for ch in s if ch.isdigit())
    if len(digits) == 4 and 1900 <= int(digits) <= 2100:
        return True
    return '-' in s and any(ch.isdigit() for ch in s)


def _parse_num_text(v) -> int:
    """
    从单元格原始文字解析整数。
    "100份 查看"→100, "1,234"→1234, "1.5万"→15000, "统计中"→0,
    "1.2w"→12000, "5k"→5000, "约100+"→100, "共 ３００"→300
    返回 int；解析失败返回 0。
    """
    import re
    s = str(v).strip()
    if not s or s.lower() in ('none', 'null', 'nan', '-', '--', '/', '统计中', '查看', '暂无', '无'):
        return 0
    # 全角数字/空格 → 半角
    s = s.translate(_FULLWIDTH_TRANS)
    s = s.replace('\u3000', ' ').replace('　', ' ')
    # 去千分位
    s = s.replace(',', '').replace('，', '')
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    if not m:
        return 0
    num = float(m.group())
    # 单位换算（支持中英文单位：亿/万/千 与 w/k，w=万）
    lower_s = s.lower()
    if '亿' in s:
        num *= 100000000
    elif '万' in s or re.search(r'[\d.]+\s*w\b', lower_s):
        num *= 10000
    elif '千' in s or re.search(r'[\d.]+\s*k\b', lower_s):
        num *= 1000
    # '+' 后缀（如 "100+"）和 约/近/共 前缀不需要额外处理，数字已提取
    return int(round(num))








def _dedup_models(*names) -> list:
    """模型列表去重（保留顺序）：用户配置模型恰好是 fallback 时避免重复请求"""
    seen = set()
    out = []
    for n in names:
        if n and n.strip():
            s = n.strip()
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _ocr_api_call(img_b64: str, prompt: str, max_tok: int = 1024,
                  forced_model: str = None, prefer_general: bool = False) -> tuple:
    """
    通用视觉 API 调用：按 settings 提供商配置选端点/模型。
    识别失败如实抛出错误，不偷偷 fallback 到其他模型（v1.3：一切如实）。
    返回 (content_text, model_used)；失败抛 RuntimeError。
    prefer_general=True：探测列/列名汇总等任务必须用通用视觉模型——
    Qwen OCR 专用模型（qwen*-ocr）只做文字提取，不做"列名汇总/结构理解"，
    会返回文字定位结果而非表格结构（v1.4 修复）。此时复用 vision._pick_vision_model
    的规则：主模型通用→用主模型；主模型 OCR→用副模型；都不可用→报错。
    """
    api_cfg = get_api_config()
    active = api_cfg.get('active_provider', 'doubao')
    providers = api_cfg.get('providers', {})
    provider = providers.get(active, {}) if isinstance(providers, dict) else {}

    # 探测/结构理解任务：若主模型是 OCR 专用，切换为通用视觉模型（vision 同款规则）
    # 复用 vision._pick_vision_model：主模型通用→用主模型；主模型 OCR→用副模型；都不可用→报错
    if prefer_general and not forced_model:
        try:
            from vision import _pick_vision_model as _pvm
            _a2, _p2, _e2, _k2, _m2, _r2 = _pvm()
            # 只要解析出的模型与主模型不同（即发生了 OCR→通用切换），就使用解析结果
            _cur_main = provider.get('model', '') or ''
            if str(_m2).strip() != str(_cur_main).strip():
                active = _a2
                provider = _p2 if isinstance(_p2, dict) else {}
                # 走下方统一逻辑：key/model/endpoint 从 provider 重新解析
                key = _k2
                model_name = _m2
                endpoint = _e2
                use_responses = _r2
                if not key:
                    raise RuntimeError(f"API Key 未设置 — 请在「API 管理」页面配置 {active} 的 Key")
                return _ocr_api_call_do(img_b64, prompt, max_tok, forced_model,
                                        active, provider, key, model_name, endpoint, use_responses)
        except Exception as _e:
            if isinstance(_e, RuntimeError):
                raise
            pass  # 解析异常则按原逻辑（可能抛"无 key"等明确错误）

    key = provider.get('api_key', '') or os.environ.get(
        {'doubao':'ARK_API_KEY','qwen':'DASHSCOPE_API_KEY','glm':'ZHIPU_API_KEY'}.get(active, ''), '')
    model_name = forced_model or provider.get('model', '')
    endpoint = provider.get('endpoint', '')
    use_responses = False

    return _ocr_api_call_do(img_b64, prompt, max_tok, forced_model,
                            active, provider, key, model_name, endpoint, use_responses)


def _extract_response_text(data: dict, mdl: str) -> str:
    """从模型 API 响应中兼容提取正文文本（v1.4 审查加固）。

    各模型/端点返回结构不统一：
    - Chat Completions: data['choices'][0]['message']['content']（str 或 list[{'type','text'}]）
    - Responses API:   data['output'][-1]['content'][0]['text']
    统一兼容：先找 choices，再找 output；content 可能是 str/list[dict]/None，
    递归提取文本片段，全部失败抛 RuntimeError 给出模型名与原始响应。
    """
    def _extract_text_part(part):
        if isinstance(part, str):
            return part
        if isinstance(part, list):
            return ''.join(_extract_text_part(p) for p in part)
        if isinstance(part, dict):
            if isinstance(part.get('text'), str):
                return part['text']
            if isinstance(part.get('content'), (str, list, dict)):
                return _extract_text_part(part.get('content'))
            return ''
        return ''

    # 1) Chat Completions 结构
    try:
        msg = data['choices'][0]['message']
        content = _extract_text_part(msg.get('content'))
        if content:
            return content
        # content 存在但为空（None/空串/空列表）：模型返回空内容是合法响应，
        # 返回空串让上层按"无结果"处理，不视为格式错误
        if 'content' in msg:
            return ''
    except (KeyError, IndexError, TypeError):
        pass
    # 2) Responses API 结构
    try:
        for out_item in reversed(data.get('output') or []):
            content = _extract_text_part(out_item.get('content'))
            if content:
                return content
    except (KeyError, IndexError, TypeError):
        pass
    # 3) 全部失败：明确报错（含响应摘要，便于排查格式变化）
    _snippet = str(data)[:200]
    raise RuntimeError(f"模型返回格式无法解析（{mdl}）: {_snippet}")


def _ocr_api_call_do(img_b64, prompt, max_tok, forced_model,
                     active, provider, key, model_name, endpoint, use_responses):
    """_ocr_api_call 的请求执行段（可被 prefer_general 复用，传入已解析配置）。"""
    providers_global = get_api_config().get('providers', {}) if callable(get_api_config) else {}
    if not isinstance(providers_global, dict):
        providers_global = {}

    # forced_model 可能属于其他 provider（如主模型 glm、副模型 qwen3.5-ocr）：
    # 按模型名前缀推断所属 provider，切换到它的 endpoint/key——否则副模型请求
    # 会发到主 provider 的 endpoint 报"模型不存在"（v1.4 修复，实测 glm 主+qwen-ocr 副报 1211）
    if forced_model:
        _fm = str(forced_model).strip().lower()
        _fm_prov = None
        if _fm.startswith('glm'):
            _fm_prov = 'glm'
        elif _fm.startswith(('qwen', 'qwen3', 'qwen-vl')):
            _fm_prov = 'qwen'
        elif _fm.startswith(('doubao', 'ep-')):
            _fm_prov = 'doubao'
        if _fm_prov and _fm_prov in providers_global and active != _fm_prov:
            _alt = providers_global.get(_fm_prov, {}) or {}
            if isinstance(_alt, dict) and (_alt.get('api_key') or _alt.get('model')):
                active = _fm_prov
                provider = _alt
                endpoint = _alt.get('endpoint', '')
                key = _alt.get('api_key', '') or os.environ.get(
                    {'doubao': 'ARK_API_KEY', 'qwen': 'DASHSCOPE_API_KEY', 'glm': 'ZHIPU_API_KEY'}.get(active, ''), '')
                model_name = forced_model or _alt.get('model', '')
                use_responses = False

    if not key:
        raise RuntimeError(f"API Key 未设置 — 请在「API 管理」页面配置 {active} 的 Key")

    # 根据提供商确定默认值，但以用户设置的 endpoint 为准
    if active == 'doubao':
        if not endpoint:
            endpoint = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
        # 根据 endpoint 判断 API 类型，而非模型名
        use_responses = ('responses' in endpoint.lower())
        if use_responses:
            # Doubao 的 model 名（如 Doubao-Seed-2.1-pro）在 ark 不一定是有效推理 ID，
            # 实测直调报 InvalidEndpointOrModel.NotFound。custom_endpoint（ep-xxx）才是有效 ID。
            custom_ep = provider.get('custom_endpoint', '')
            models = _dedup_models(custom_ep or model_name)
        else:
            models = _dedup_models(provider.get('custom_endpoint', '') or model_name)
    elif active == 'qwen':
        if not endpoint:
            endpoint = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        models = [model_name or 'qwen3.5-omni-flash']
    else:  # glm
        if not endpoint:
            endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        models = [model_name or 'glm-4v-flash']

    for attempt, mdl in enumerate(models):
        _check_cancel()  # v1.4.2 紧急停止：F9 后重试/换模型前立即中断
        mdl = mdl.strip()  # 用户可能输入带前后空格的模型名，发送前清理
        # v1.4.2 输出上限分档：弱模型（glm-4v-flash=1024/未知=2048）期望越大越容易
        # 400；qwen 系/OCR 系给足预算（4096/8192）。替代旧的仅 glm 1024 特判。
        cur_max_tok = _pick_max_tok(mdl, max_tok)
        # 如果fallback是智谱模型但当前走的是阿里/豆包端点，切换endpoint + 格式
        # 模型名/端点判断统一小写化，避免用户配 "GLM-4V-Flash"/"Responses" 时匹配失败
        cur_endpoint = endpoint
        cur_key = key
        cur_responses = use_responses
        mdl_l = mdl.lower()
        ep_l = cur_endpoint.lower()
        # 精确/前缀匹配智谱模型名，避免自定义模型名含 "glm" 子串被误判
        is_glm = mdl_l.startswith('glm-') or mdl_l == 'glm'
        # 自动切智谱端点只对官方默认端点生效（dashscope.aliyuncs.com / ark 官方域名），
        # 自定义代理 endpoint 一律尊重用户配置——代理 URL 可能恰好含 'dashscope'/'ark'/
        # 'responses' 子串，按子串判断会错换 endpoint+key 导致认证失败（v1.4 加固）
        _is_official_ali = 'dashscope.aliyuncs.com' in ep_l
        _is_official_ark = 'ark.cn-beijing.volces.com' in ep_l
        if is_glm and (_is_official_ali or _is_official_ark):
            cur_endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
            cur_key = providers_global.get('glm', {}).get('api_key', '') if isinstance(providers_global, dict) else ''
            if not cur_key:
                continue
            cur_responses = False
        # v1.4.2 请求重发：输出上限超模型限制（400/token 错误）自动砍半重发一次（不换模型）
        _tok_downgraded = False
        while True:
            _check_cancel()
            try:
                if cur_responses and mdl != 'glm-4v-flash':
                    # Responses API（Doubao-Seed-2.1-pro：thinking:disabled + 图已预压缩）
                    resp = requests.post(cur_endpoint,
                            headers={'Authorization': f'Bearer {cur_key}', 'Content-Type': 'application/json'},
                        json={
                            'model': mdl,
                            'thinking': {'type': 'disabled'},
                            'input': [{'role': 'user', 'content': [
                                {'type': 'input_image', 'image_url': f'data:image/jpeg;base64,{img_b64}', 'detail': 'low'},
                                {'type': 'input_text', 'text': prompt}
                            ]}],
                            'temperature': 0.0,
                            # Responses API 规范的输出长度限制参数（区别于 Chat Completions 的 max_tokens）
                            'max_output_tokens': cur_max_tok,
                            'stream': False
                        }, timeout=(10, 180))  # v1.4.2 读取超时 30s→180s：VL 处理大图(9列大表)可达 60-120s，
                        #  30s 必然误判失败（客户实测小表3-5行成功、大表必超时）——给足处理时间再谈网络
                    data = resp.json()
                    if not data.get('output'):
                        raise RuntimeError(f"OCR失败（{mdl}）: {data}")
                    # output[-1] = 最后一条消息（跳过 reasoning）；兼容 content 为
                    # str / list[dict] / None 的结构差异（v1.4 审查加固）
                    content = _extract_response_text(data, mdl)
                else:
                    # Chat Completions 分支：GLM-4.6v 默认开 reasoning 会吃满 max_tokens 导致正文截断，
                    # 必须显式禁用 thinking；glm-4v-flash 无 reasoning 但同样接受该参数。
                    # 其他模型（doubao/qwen）也发，保证一致。
                    cc_payload = {
                        'model': mdl,
                        'messages': [{'role': 'user', 'content': [
                            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
                            {'type': 'text', 'text': prompt}
                        ]}],
                        'temperature': 0.0, 'max_tokens': cur_max_tok,
                        'thinking': {'type': 'disabled'},
                    }
                    resp = requests.post(cur_endpoint,
                            headers={'Authorization': f'Bearer {cur_key}', 'Content-Type': 'application/json'},
                        json=cc_payload, timeout=(10, 180))  # v1.4.2 读取超时 30s→180s，理由同上（VL 大图处理耗时）
                    data = resp.json()
                    if not data.get('choices'):
                        raise RuntimeError(f"OCR失败（{mdl}）: {data}")
                    # Chat Completions content 可能为 str 或 list[{'type','text'}] 或 None，
                    # 统一兼容解析（v1.4 审查加固）
                    content = _extract_response_text(data, mdl)
                return content, mdl
            except Exception as e:
                # 输出 token 上限超模型能力（弱模型/模型版本限制）：400 或
                # 'maximum context length'/'max_tokens' 类错误 → 砍半重发一次
                _es = str(e).lower()
                # v1.4.2 降档条件收紧（find-bugs ③）：只对明确的输出 token 超限错误
                # 砍半重发——裸 '400' 可能是"模型不存在/其他参数错误"，不该混成降档
                if ((('token' in _es and ('max' in _es or 'limit' in _es or 'exceed' in _es or 'length' in _es)
                      and '400' in _es)
                     or 'maximum context' in _es or 'max_tokens' in _es or 'output tokens' in _es)
                        and cur_max_tok > 256 and not _tok_downgraded):
                    _tok_downgraded = True
                    cur_max_tok = max(256, cur_max_tok // 2)
                    try:
                        _ocr_dlog(f"⚠ 输出上限超模型限制，砍半为 {cur_max_tok} 重发: {str(e)[:80]}")
                    except Exception:
                        pass
                    continue
                if isinstance(e, json.JSONDecodeError):
                    raise RuntimeError(f"模型返回无法解析的内容（{mdl}）：{e}")
                if isinstance(e, RuntimeError):
                    raise
                raise RuntimeError(f"模型调用失败（{mdl}）：{e}")

    raise RuntimeError(f"没有可用的识别模型（active={active}）")


# 行政后缀，按长度降序（先匹配长后缀，避免「壮族自治区」只被「自治区」截断）
REGION_SUFFIXES = ['特别行政区', '维吾尔自治区', '壮族自治区', '回族自治区',
                   '自治区', '省', '市']


def strip_region_suffix(region: str) -> str:
    """地区名去行政后缀：云南省→云南，北京市→北京，广西壮族自治区→广西。"""
    if not region:
        return ''
    r = str(region).strip()
    for sfx in REGION_SUFFIXES:
        if r.endswith(sfx):
            return r[:-len(sfx)]
    return r


# 词条噪音：后台单元格下方/旁边的链接文字（「查看地址」「查看」）会被 OCR 连进单元格值。
# 词条噪音：后台单元格下方/旁边的链接文字（「查看地址」「查看」「更新记录」）会被 OCR 连进单元格值。
# 识别不稳定：有时带空格、有时换行、有时粘连、有时只识别出半截、偶尔单字误识别
# （实测「查看地址」→「竞看地址」），所以按尾部词条做编辑距离容差剥离。
# 长词在前：先剥「查看地址」再剥「查看」，避免只剥「查看」留下「地址」残片。
TAIL_NOISE_WORDS = ('查看地址', '更新记录', '查看')

# 尾部日期时间噪音（「仓库预估总销售数」等列常带更新时间：'65份 08-04 15:36'）
# 匹配 月-日 / 年-月-日（-、/、年月日分隔），可选 时:分[:秒]，允许前导/尾随空白。
_TAIL_DATETIME_RE = r'(?:[ \t\u3000\r\n]*)(?:(?:\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)|(?:\d{1,2}[-/月]\d{1,2}日?))(?:[ \t]+[0-2]?\d:[0-5]\d(?::[0-5]\d)?)?[ \t\u3000\r\n]*$'

# 尾部多余纯数字串（豆包/glm 偶发把相邻数字/时间识别成纯数字接在数值后：'102份 12345'）
# 只剥"空白 + 纯数字"结尾；数字前必须有内容（空白分隔）。
# 注意：纯数字值本身（如库存 '85'）前无空白，**不能剥**——否则刷新计算后仓库总库存被清空
_TAIL_NUM_RE = r'[ \t\u3000\r\n]\d+(?:[.,，、]\d+)*[ \t\u3000\r\n]*$'


def _ocr_dlog(msg: str):
    """轻量诊断日志：写入 get_base_dir()/output/ocr_dlog.txt（output/ 已 gitignore）。"""
    try:
        import os
        _p = os.path.join(get_base_dir(), 'output', 'ocr_dlog.txt')
        os.makedirs(os.path.dirname(_p), exist_ok=True)
        with open(_p, 'a', encoding='utf-8') as _f:
            _f.write(msg + '\n')
    except Exception:
        pass


def _lev(a, b) -> int:
    """编辑距离（Levenshtein），简单 DP。"""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _match_tail_noise(s: str, word: str):
    """检查 s 尾部是否近似词条 word；返回剥离起点（词条起始 index），无则 None。

    容差：尾部窗口长度与词条差 ≤1 且编辑距离 ≤1——
    覆盖 OCR 单字误识别（查→竞/香/茶）、粘连（无空白）、少字/多字。
    多个候选取 (距离, 长度) 最优：距离小的优先（精确词条优先于近似），
    同距离取更长窗口（连前导空白一起剥干净），避免「份查看」被误判成近似词条。
    """
    n = len(s)
    wlen = len(word)
    best = None
    for L in range(max(1, wlen - 1), min(n, wlen + 1) + 1):
        start = n - L
        cand = s[start:].strip()
        if not cand:
            continue
        if abs(len(cand) - wlen) > 1:
            continue
        dist = _lev(cand, word)
        if dist <= 1 and (best is None or dist < best[0] or (dist == best[0] and L > best[1])):
            best = (dist, L, start)
    return best[2] if best else None


def strip_tail_noise(value) -> str:
    """剥离值尾部的词条/日期时间噪音，只删噪音形态、不误伤名称（含 OCR 单字误识别容差）。

    例：'128份 查看' → '128份'，'128份查看' → '128份'，
        '示例仓库查看地址' → '示例仓库'，'示例仓库 竞看地址' → '示例仓库'（查→竞），
        '65份\\n08-04 15:36\\n更新记录' → '65份'，'128份 08-02' → '128份'，
        '查看地址' → ''；名称中非尾部的「查看」（如"查看库存"）不受影响。
    v1.4.5（bug hunt F4）：整值剥空保护——当原值本身整体呈噪音形态（如'更新时间'列
    的纯日期 '2024-05-04'、年月日），剥离后为空时保留原文，避免整列被清空。"""
    if value is None:
        return ''
    import re as _re
    s = str(value).strip()
    changed = True
    while changed:
        changed = False
        for word in TAIL_NOISE_WORDS:
            start = _match_tail_noise(s, word)
            if start is not None:
                s = s[:start].rstrip()
                changed = True
        m = _re.search(_TAIL_DATETIME_RE, s)
        if m:
            s = s[:m.start()].rstrip()
            changed = True
        m = _re.search(_TAIL_NUM_RE, s)
        if m:
            s = s[:m.start()].rstrip()
            changed = True
    _out = _re.sub(r'[ \t\u3000\r\n]+', ' ', s).strip()
    if not _out and str(value).strip():
        # v1.4.5（bug hunt F4）整值保护（验收回归 N1/C7 收窄）：仅当原值呈"日期/数字形态"
        # 才保留原文（防 '2024-05-04' 这类真实数据被清空）；纯词条噪音（'查看地址'/
        # '更新记录'）仍剥空，避免噪音文本入库/导出
        _orig = str(value).strip()
        _dateish = bool(_re.search(r'\d{4}\s*[年.\-/]|\d{1,2}\s*[月/\-]\s*\d{1,2}|\d{4}|\d{1,2}:\d{2}', _orig))
        if _dateish:
            return _orig
        return ''
    return _out


def dedup_items(items, seen_sku, seen_name_no_sku, seen_name_with_id):
    """按 sku_id 权威去重（无 ID 回退 name），返回去重后的新条目；就地更新三个 seen 集合。

    滚动加载多轮截图时，同一商品会反复出现；商品名有 OCR 单字波动（结→丝），
    **sku_id 长数字串每轮也可能错 1~2 位**（实测 11111111111→11111198811、22222222222→28822222222），
    极端时整段错位（33333333333→44444444444，编辑距离远超 2）。所以：
      - ID 精确命中 → 同商品去重；
      - ID 近匹配（编辑距离≤2）且 name 前 6 字相同 + 总距离≤6 → 同商品（OCR 数字错位）；
      - **OR name 强相似（前 6 字相同 + 总距离≤6）→ 同商品**（sku 整段错位时靠 name 兜底；
        商品名比 sku 稳定——同商品核心名稳定、尾部描述词乱，相邻商品前缀通常不同不会误并）；
      - 无 ID → name 精确 + 模糊去重（前 6 字相同 + 距离≤2，防滚动轮 OCR 波动漏拦）。
    覆盖场景：同名不同ID保留 / 先无ID后有ID拦截 / 先有ID后无ID拦截 / 无ID同名去重 / 同ID名字波动 /
    **同商品ID错位波动** / **同商品sku整段错位但name相似** / **同商品多仓各自独立（v1.4.2）**。
    seen_sku 为 dict {sku_id: {仓库: name}}（多仓映射，name 佐证）。
    """
    out = []
    for it in items:
        nm = it.get('name', '')
        sku = it.get('sku_id', '')
        if not nm:
            continue
        if sku:
            # v1.4.2：同 ID 不同仓库 → 不同记录（同商品多仓各自独立，客户场景），
            # 仅 同 ID 同仓库 才算滚动重复。seen_sku 值升级为 {warehouse: name}
            _prev_map = seen_sku.get(sku) or {}
            if not isinstance(_prev_map, dict):
                _prev_map = {}
            if it.get('warehouse') in _prev_map:
                continue  # 同 ID 同仓库已见（滚动重复）
            # 模糊兜底：同商品三路判断（仓库感知——只与本商品仓库的已见记录比）
            hit = False
            for _s, _n in seen_sku.items():
                _n_map = _n if isinstance(_n, dict) else {}
                _n_name = _n_map.get(it.get('warehouse'))
                if _n_name is None:
                    continue  # 该已见记录的仓库 ≠ 本商品仓库（多仓不互去重）
                _id_near = abs(len(_s) - len(sku)) <= 2 and _lev(_s, sku) <= 2
                _name_prefix = _n_name[:6] == nm[:6]
                if _id_near and _name_prefix:
                    hit = True
                    break
                if not _id_near and _n_name != nm and _name_prefix and _lev(_n_name, nm) <= 6:
                    # name 强相似兜底（sku 整段错位时靠 name）：距离≤2 视为同商品去重；
                    # 距离 3~6 只标记不删除——不同规格商品（500ml vs 1L）前缀相同且
                    # 编辑距离可能在 3~6，自动删会合并错库存（v1.4 审查收紧）
                    if _lev(_n_name, nm) <= 2:
                        hit = True
                    else:
                        it['_dup_suspect'] = True
                        it['_dup_suspect_with'] = _s
                    break
            if hit:
                continue
            if not isinstance(seen_sku.get(sku), dict):
                seen_sku[sku] = {}
            seen_sku[sku][it.get('warehouse')] = nm
            # v1.4.2：name 交叉拦截带仓库——同 name 不同仓库不互拦
            if (nm, it.get('warehouse')) in seen_name_no_sku:
                continue
            seen_name_with_id.add((nm, it.get('warehouse')))
        else:
            _wh = it.get('warehouse')
            # 无 ID 商品：精确 + 模糊去重（v1.4.1 修复：客户滚动轮 name OCR
            # 波动 1~2 字时精确匹配漏拦，同一商品反复入列 → 4 商品识别出 7 个。
            # 阈值与有 ID 商品 name 强相似一致：前 6 字相同 + 编辑距离 ≤2；
            # 距离 3~6 不删（无 ID 不同规格商品前缀相同，误并会合并错库存）。
            # v1.4.2：seen 集合带仓库——同 name 不同仓库各自保留（多仓不互去重）
            if (nm, _wh) in seen_name_no_sku or (nm, _wh) in seen_name_with_id:
                continue
            _nm_hit = False
            for (_sn, _sw) in seen_name_no_sku:
                if _sw == _wh and _sn[:6] == nm[:6] and _lev(_sn, nm) <= 2:
                    _nm_hit = True
                    break
            if not _nm_hit:
                for (_sn, _sw) in seen_name_with_id:
                    if _sw == _wh and _sn[:6] == nm[:6] and _lev(_sn, nm) <= 2:
                        _nm_hit = True
                        break
            if _nm_hit:
                continue
            seen_name_no_sku.add((nm, _wh))
        out.append(it)
    return out


def strip_warehouse_noise(warehouse: str) -> str:
    """仓库信息去词条噪音（兼容别名，走通用 strip_tail_noise）。

    例：'示例仓库 查看地址' → '示例仓库'，'示例仓库\\n查看地址' → '示例仓库'。
    """
    return strip_tail_noise(warehouse)


def normalize_col_name(name) -> str:
    """
    列名归一化：去空白/全角空格，用于客户勾选列与模型返回列名匹配。
    例：'商品名称 ' → '商品名称'，'　仓库总库存' → '仓库总库存'
    """
    if name is None:
        return ''
    s = str(name).strip().replace('\u3000', ' ').replace(' ', '')
    return s


def _match_col_name(col: str, candidates: dict, norm_map: dict):
    """列名匹配：先精确（normalize 后），失败用编辑距离≤2 + 长度差≤2 兜底。
    全列模式下模型可能把列名抄错 1~2 字（实测'仓库预估总销售数'→'仓库预达总销数'、
    '商家报价'→'商品报价'），精确匹配会漏字段 → 返回业务字段名或 None。
    **name 字段永不参与模糊匹配**（'商品信息/商品名称/商品编码/商品报价'首2字相同
    编辑距离≤2，模糊会误配核心商品名列），name 由调用方精确匹配。"""
    _norm = normalize_col_name(col)
    if _norm in norm_map and norm_map[_norm] != 'name':
        return (norm_map[_norm], 1)  # 精确命中：高置信
    # 模糊兜底：与每个已配置列名比较，编辑距离小且长度接近的命中
    best, best_d = None, 99
    for _cfg_col, _field in norm_map.items():
        if _field == 'name':
            continue  # name 永不模糊匹配
        if abs(len(_cfg_col) - len(_norm)) > 2:
            continue
        _d = _lev(_cfg_col, _norm)
        # 只允许 ≤2 字差异（预估→预达、销售数→总销数）；
        # 但前 3 字必须相同——「销售规格/销售日期」前3字是"销售规/销售日"，
        # 与「销售区域」的"销售区"不同，不会误配；「仓库销售库存」前3字"仓库销"
        # 与「仓库总库存」的"仓库总"不同，也不会误配（v1.4 收紧）
        if _d <= 2 and _d < best_d:
            if len(_cfg_col) >= 3 and len(_norm) >= 3 and _cfg_col[:3] != _norm[:3]:
                continue
            best, best_d = _field, _d
    # 距离 2 属于低置信匹配（仓库销售库存 vs 仓库总库存 距离 2 但业务列不同），
    # 调用方需要据此标记低置信供用户复核（v1.4 审查修复）
    if best is not None and best_d >= 2:
        return (best, 2)
    return (best, 1) if best is not None else None


_SKU_ID_RE = None  # 延迟初始化（正则编译一次）


def _split_name_id(value: str) -> tuple:
    """
    拆分商品信息列的值 → (商品名, sku_id)。
    后台格式：'示例商品A500g/袋 ID:12345678901' → ('示例商品A500g/袋', '12345678901')
    支持 ID:xxx / 商品ID:xxx / id=xxx / #xxx 等常见格式；无 ID 时返回 (原值, '')。
    """
    global _SKU_ID_RE
    if _SKU_ID_RE is None:
        import re as _re
        _SKU_ID_RE = _re.compile(
            r'(?:商品ID|商品id|sku\s*id|sku_id|(?<![A-Za-z0-9])ID|(?<![A-Za-z0-9])id)'
            r'\s*[:：=#]?\s*(\d{5,})', _re.IGNORECASE)
    s = str(value or '').strip()
    if not s:
        return '', ''
    m = _SKU_ID_RE.search(s)
    if m:
        sku_id = m.group(1)
        name = (s[:m.start()] + s[m.end():]).strip(' \t\r\n-—|·,，。')
        # 清理名称里残留的 ID 标记碎片
        name = name.replace('商品ID', '').replace('sku_id', '').replace('ID:', '').strip()
        return name, sku_id
    return s, ''


def map_columns_to_fields(row: dict, mapping: dict) -> dict:
    """
    把通用列行 {列名: 值} 按映射转成业务字段 {name, stock, sales, region, warehouse, sku_id}。
    mapping: {'name': '商品信息', 'stock': '仓库总库存', 'sales': '仓库预估总销售数',
              'region': '销售区域', 'warehouse': '仓库信息'}
    列名匹配用 normalize_col_name 归一化（容忍模型返回列名的细微差异）。
    商品信息列值含 ID 时拆分为 name + sku_id（重名商品靠 ID 去重）。
    返回带原始列数据的 dict：{name, stock, sales, region, warehouse, sku_id, _raw: {列名: 值}}。
    """
    norm_map = {}
    for field, col in (mapping or {}).items():
        if col:
            norm_map[normalize_col_name(col)] = field
    # v1.4.2 列名别名兼容：PDD 页面列名随版本变化（实测"仓库销售库存" 与
    # "仓库预估总销售数" 是同一业务列的不同页面版本），用户映射是主名时，
    # 把该字段的全部已知别名也加入匹配（防止 sales 解析为 0 → 补货计算全乱）；
    # 用户自定义列名（非主名/别名）不套用别名，尊重显式配置。
    _FIELD_ALIASES = {
        'sales': ('仓库预估总销售数', '仓库销售库存', '仓库预估总销量'),
        'stock': ('仓库总库存', '仓库库存'),
    }
    for _field, _alts in _FIELD_ALIASES.items():
        _cfg = (mapping or {}).get(_field) or ''
        _cfg_norm = normalize_col_name(_cfg)
        if _cfg_norm in {normalize_col_name(a) for a in _alts}:
            for _a in _alts:
                norm_map.setdefault(normalize_col_name(_a), _field)
    out = {'name': '', 'stock': '', 'sales': '', 'region': '', 'warehouse': '',
           'sku_id': '', '_raw': dict(row)}
    for col, val in row.items():
        # name 列必须精确匹配（商品名是核心字段，模糊会误配"商品编码/商品报价"）；
        # 其他字段允许编辑距离≤2 模糊（全列模式列名可能抄错 1~2 字，v1.4 修复）
        _norm_col = normalize_col_name(col)
        field = norm_map.get(_norm_col)
        _col_conf = 1  # 1=精确/高置信，2=模糊低置信（供 _low_confidence 标记）
        if field is None and _norm_col != normalize_col_name(mapping.get('name', '')):
            _m = _match_col_name(col, norm_map, norm_map)
            if _m:
                field, _col_conf = _m
        if field and field in ('name', 'stock', 'sales', 'region', 'warehouse'):
            out[field] = '' if val is None else str(val)
            if _col_conf >= 2:
                out['_low_conf_col'] = col  # 模糊匹配的列，供调用方提示用户复核
    # 商品信息列拆分：name + sku_id
    if out.get('name'):
        _name, _sku = _split_name_id(out['name'])
        out['name'] = _name
        out['sku_id'] = _sku
    return out


def parse_items_generic(rows: list, mapping: dict) -> list:
    """
    通用行 → 业务字段列表（含原始列数据）。
    每行转成 {name, stock, sales, region, warehouse, _raw}，数字列用 _parse_num_text 解析。
    供 GUI 计算使用；跨仓库去重等仍按 name 语义（客户映射里应指定商品名列）。
    """
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mapped = map_columns_to_fields(row, mapping)
        name = mapped.get('name', '').strip()
        if not name:
            continue  # 无商品名 → 跳过该行（映射未配商品名列时可能全空）
        if not mapped.get('sku_id'):
            # 无商品 ID 不再整行丢弃：OCR 场景 ID 可能因字号太小漏识别，
            # 商品名正确时整行删除会丢真实数据（v1.4 审查修复）——
            # 标记 _missing_id 保留，GUI/去重阶段决定展示与提示
            mapped['_missing_id'] = True
            _ocr_dlog(f"⚠ 无商品ID已标记: {name[:40]}")
        mapped['stock'] = _parse_num_text(mapped.get('stock', ''))
        mapped['sales'] = _parse_num_text(mapped.get('sales', ''))
        mapped['region'] = strip_region_suffix(mapped.get('region', ''))
        mapped['warehouse'] = strip_warehouse_noise(mapped.get('warehouse', ''))
        items.append(mapped)
    return items


def _write_ocr_debug(cols, rows, note=''):
    """调试：把模型返回的表头与行样本**累积**写到可写目录，排查列错位/滚动重复。
    滚动多轮每次 OCR 都追加一条（保留最近 40 条，带时间戳），可对比每轮 sku_id 是否稳定。
    源码运行 → output/_ocr_debug.json；打包后 → %APPDATA%/PDD补货助手/output/_ocr_debug.json。"""
    try:
        _d = os.path.join(get_base_dir(), 'output')
        os.makedirs(_d, exist_ok=True)
        import time as _t
        _rec = {'ts': _t.strftime('%H:%M:%S'), 'note': note, 'columns': cols,
                'rows_sample': rows[:5]}
        _p = os.path.join(_d, '_ocr_debug.json')
        try:
            with open(_p, 'r', encoding='utf-8') as _f:
                _hist = json.load(_f)
            if not isinstance(_hist, list):
                _hist = [_hist]
        except Exception:
            _hist = []
        _hist.append(_rec)
        with open(_p, 'w', encoding='utf-8') as _f:
            json.dump(_hist[-40:], _f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def ocr_table(image_path: str, columns: list = None, forced_model: str = None,
              table_bbox: dict = None, prefer_general: bool = False) -> dict:
    """
    通用表格 OCR：识别 PDD 后台表格的任意列（不再局限于固定商品字段）。

    columns=None → 探测模式：识别表头所有列 + 每行每列的值。
    columns=[...] → 指定列模式：只识别指定列（客户勾选的列）。

    返回 {'columns': [列名...], 'rows': [{列名: 单元格原文, ...}, ...]}
    失败/无数据抛 RuntimeError（调用方处理）。
    """
    # Qwen OCR 专用模型：支持大图保留小字细节 + 默认 4096 token（识别表格更长更稳）。
    # 检测 forced_model 或当前 provider model 是否为 qwen*-ocr 系列。
    _is_ocr = False
    try:
        from utils import get_api_config as _gac
        _acfg = _gac()
        _ap = _acfg.get('active_provider', '')
        _pm = ((_acfg.get('providers') or {}).get(_ap, {}) or {}).get('model', '')
        _is_ocr = _is_qwen_ocr(forced_model) or _is_qwen_ocr(_pm)
    except Exception:
        _is_ocr = False
    img_b64 = _prep_image_b64(image_path, table_bbox=table_bbox,
                              max_side=2560 if _is_ocr else 1920)

    if columns:
        cols_txt = '、'.join(str(c) for c in columns)
        # 示例 JSON 先整体序列化（列名可能含引号/花括号，json.dumps 保证安全且不破坏 f-string 语法）
        _ex_col1 = columns[0]
        _ex_col2 = columns[1] if len(columns) > 1 else '库存'
        _example_json = json.dumps({_ex_col1: "示例商品B500g", _ex_col2: "128份"},
                                   ensure_ascii=False)
        prompt = f"""你是数据录入员，识别图中 PDD 后台表格。表格为竖向列表，每行一条数据。

只识别以下列（严格按这些列名作为 JSON key，列名原样）：{cols_txt}

输出要求：
1. 严格按表格从上到下顺序逐行输出，一行不漏、不重复、不合并
2. 每行输出一个 JSON 对象，key 用上面给的列名原样（缺某列的值填 null，不要编造）
3. 单元格值原样抄写，不要转换数字、不要去掉单位；数字后的日期时间不抄（如"258份 08-02"只抄"258份"）。**数字类列（库存/销量）只抄第一个数值，数值后如果还有其他数字串/时间，一律不要抄**（如"102份 12345"只抄"102份"）。**如果数字看起来异常多位（如 1109、100000），要核对是否把其他文字/格式读进去了，数字通常是简洁的整数**；**数字必须完整输出——末位 0 必须保留（如 1230 不能写成 123）、禁止丢位/截断/省略（v1.4.2 数字完整性强化）**
4. 值为 0 是真实业务数据，该行必须保留，绝不能跳过
5. 商品信息类列（如「商品信息」「商品名称」）包含商品名和商品ID（如"示例商品A500g/袋 ID:12345678901"），必须完整原样抄写，不得去掉 ID 部分——商品ID用于区分重名商品。商品名**逐字原样抄写**，禁止用形近字/同音字替换（如"结"写成"丝"、"己"写成"已"），看不清的宁可填 null。**ID 是纯数字串（ID: 后跟一串数字），必须逐位核对，数字识别不清时宁可省略 ID 也不要编造/改位**
6. 整张截图没有有效表格时只输出 []
7. 只输出 JSON 数组，不要任何解释文字

示例（仅示意格式）：
[{_example_json}]"""
        # 列多时按列数放大 token，但设 8192 上限防止过度消耗 API 额度；
        # Qwen OCR 专用模型默认支持 4096+，直接吃满输出预算（表格识别更长更稳）
        if _is_ocr:
            max_tok = 4096
        else:
            max_tok = min(max(1024, 512 * max(1, len(columns))), 8192)
    else:
        prompt = """你是数据录入员，识别图中 PDD 后台表格。表格为竖向列表，每行一条数据。

请识别：
1. 表格的所有列名（表头），保持从左到右顺序，列名原样抄写（如"商品名称""仓库总库存"）
2. 每一行的所有单元格值，按列对应

输出严格 JSON（只输出 JSON，不要解释）：
{"columns": ["列名1", "列名2", ...], "rows": [{"列名1": "值", "列名2": "值", ...}, ...]}

要求：
- columns 与表头完全一致（顺序、文字原样）
- rows 每行一个对象，key 必须与 columns 完全一致
- 单元格值原样抄写，不要转换数字、不要去掉单位；数字后的日期时间不抄。**数字类列（库存/销量）只抄第一个数值，数值后如果还有其他数字串/时间，一律不要抄**（如"102份 12345"只抄"102份"）。**如果数字看起来异常多位（如 1109、100000），要核对是否把其他文字/格式读进去了，数字通常是简洁的整数**；**数字必须完整输出——末位 0 必须保留（如 1230 不能写成 123）、禁止丢位/截断/省略（v1.4.2 数字完整性强化）**
- 商品信息类列（如「商品信息」）含商品名和商品ID（如"示例商品A500g/袋 ID:12345678901"），必须完整原样抄写，不得去掉 ID 部分。商品名**逐字原样抄写**，禁止用形近字/同音字替换（如"结"写成"丝"、"己"写成"已"），看不清的宁可填 null。**ID 是纯数字串，必须逐位核对，数字识别不清时宁可省略 ID 也不要编造/改位**
- 值为 0 是真实业务数据，必须保留该行
- 无法识别的单元格填 null，不要编造
- 表格为空或无有效数据时输出 {"columns": [], "rows": []}"""
        max_tok = 4096  # v1.4.2：全列大表（9行14列≈2500token）1024/2048 必截断（客户实测 JSON 断尾）；
        # desired 4096 由 _ocr_api_call_do 按模型分档钳制——qwen 系/OCR 系 4096、
        # glm-4v-flash 1024、未知模型 2048，弱模型不会 400。

    content, _ = _ocr_api_call(img_b64, prompt, max_tok=max_tok, forced_model=forced_model,
                               prefer_general=prefer_general)

    # 解析复用 _parse_ocr_response（v1.4.2 抽取共用：兼容 ```json 包裹/
    # {columns,rows}/纯数组/数组行对齐/key 多数票/不可哈希过滤/调试记录）
    return _parse_ocr_response(content)


def _parse_ocr_response(content: str) -> dict:
    """解析模型 OCR 响应 → {'columns': [...], 'rows': [...]}。
    ocr_table / ocr_table_verify 共用（v1.4.2 抽取，二次择优复用同一套容错解析）：
    兼容 ```json 包裹、{columns,rows} 结构、纯数组、数组行对齐、
    行 key 多数票重定列、不可哈希 key 过滤、调试记录。
    """
    text = content.strip()
    if '```' in text:
        parts = text.split('```')
        for p in parts:
            p = p.strip()
            if p.startswith('json'):
                p = p[4:].strip()
            if p.startswith('{') or p.startswith('['):
                text = p
                break
    try:
        if text.startswith('{'):
            data = json.loads(text)
            cols = data.get('columns') or []
            rows = data.get('rows') or data.get('items') or []
            # rows 可能是数组格式 [["a","b"],...]（glm-4.6v 偶发），按 columns 对齐转 dict
            if cols and rows:
                _n = len(cols)
                def _norm_row(r):
                    if isinstance(r, list):
                        return dict(zip(cols, (r + [None] * _n)[:_n]))
                    return r
                rows = [_norm_row(r) for r in rows]
            dict_rows = [r for r in rows if isinstance(r, dict) and r]
            if dict_rows:
                from collections import Counter as _C
                _safe_rows = []
                for _r in dict_rows:
                    _k2 = [k for k in _r.keys() if isinstance(k, (str, int, float, bool, type(None)))]
                    if _k2:
                        _safe_rows.append({_k: _r[_k] for _k in _k2})
                dict_rows = _safe_rows
            if dict_rows:
                _kc = _C(tuple(r.keys()) for r in dict_rows)
                _top_keys = list(_kc.most_common(1)[0][0])
                if set(cols) != set(_top_keys):
                    cols = _top_keys
                elif list(cols) != _top_keys:
                    cols = _top_keys
            _write_ocr_debug(cols, rows)
            return {'columns': list(cols), 'rows': list(rows)}
        rows = json.loads(text)
        if rows and isinstance(rows[0], dict):
            cols = list(rows[0].keys())
            _write_ocr_debug(cols, rows)
            return {'columns': cols, 'rows': rows}
        _write_ocr_debug([], rows, 'no_columns')
        return {'columns': [], 'rows': []}
    except json.JSONDecodeError:
        # v1.4.2 解析容错：先尝试从半截/带杂质文本捞回完整行（网络截断/模型夹杂
        # 说明文字时常见），能救回几行就绝不整轮归零；救不回再抛错并落盘原文。
        _rec = _recover_partial_json(text)
        if _rec and _rec[0] > 0:
            _rows_n, _cols, _rows = _rec
            try:
                _ocr_dlog(f"⚠ JSON 截断容错恢复：捞回 {_rows_n} 行（原始 {len(text)} 字符）")
            except Exception:
                pass
            _write_ocr_debug(_cols, _rows, 'recovered_partial')
            return {'columns': _cols, 'rows': _rows}
        _write_ocr_fail(text, 'JSONDecodeError')
        raise RuntimeError(f"模型返回无法解析的 JSON（前120字: {text[:120]!r}）")


def ocr_table_verify(image_path: str, table_bbox: dict = None,
                     forced_model: str = None) -> dict:
    """二次推理择优识别（v1.4.2 手机流程【7】容错机制）：主识别出现
    无商品 ID / 低置信列时调用，强化 prompt 专注 ID 逐位与数字完整性，
    返回 {'columns': [...], 'rows': [...]}（与 ocr_table 同结构）。
    只在质量信号触发时使用，避免常规路径成本翻倍。
    """
    img_b64 = _prep_image_b64(image_path, table_bbox=table_bbox,
                              max_side=2560 if _is_qwen_ocr(forced_model) else 1920)
    prompt = """你是数据录入员，识别图中 PDD 后台表格的每一行数据（竖向列表）。
识别每一行的所有列：JSON key 用图中表头列名原样（如"商品信息""仓库总库存""仓库预估总销售数""仓库信息"）。
输出严格 JSON：{"rows": [{"商品信息": "值", "仓库总库存": "值", ...}, ...]}
重点要求（本识别用于补全首轮缺漏）：
1. **商品信息列必须完整抄写商品名 + ID 数字串**（如"示例商品A500g/袋 ID:12345678901"），ID 是纯数字串，必须逐位核对输出；看不清宁可填 null 也不要编造/改位
2. **数字类列（库存/销量）完整输出，末位 0 必须保留（如 1230 不能写成 123），禁止丢位/截断/省略**
3. 按图中从上到下顺序逐行输出，一行不漏；某列缺值填 null，不要编造
4. 只输出 JSON，不要解释"""
    try:
        content, _ = _ocr_api_call(img_b64, prompt, max_tok=4096, forced_model=forced_model)
        return _parse_ocr_response(content)
    except Exception:
        raise


def merge_verify_items(items: list, verify_items: list) -> list:
    """二次识别择优（v1.4.2 手机流程【7】）：主识别中缺 ID/低置信列的行，
    用二次识别同名行的完整数据补全（ID、空数字）。返回补全后的新列表。
    匹配按 name 精确（strip+小写）；二次识别没有同名行则保持原样。
    """
    if not verify_items:
        return items
    v_by_name = {}
    for v in verify_items:
        nm = str(v.get('name') or '').replace(' ', '').lower()
        if nm and nm not in v_by_name:
            v_by_name[nm] = v
    out = []
    for it in items:
        need = bool(it.get('_missing_id') or it.get('_low_conf_col')
                    or _suspect_number(it.get('stock')) or _suspect_number(it.get('sales')))
        if need:
            nm = str(it.get('name') or '').replace(' ', '').lower()
            v = v_by_name.get(nm)
            if v:
                fixed = False
                if not it.get('sku_id') and v.get('sku_id'):
                    it['sku_id'] = v['sku_id']
                    it.pop('_missing_id', None)
                    fixed = True
                for _f in ('stock', 'sales'):
                    a = str(it.get(_f) or '')
                    b = str(v.get(_f) or '')
                    # v1.4.5（bug hunt F8 + 验收回归 N2/C8）：仅真空缺（''）才用副模型补全——
                    # 真实库存/销量为 0 是合法业务值，不得被副模型误读值覆盖
                    if a == '' and b and b != '0':
                        it[_f] = v[_f]
                        fixed = True
                if fixed:
                    it.pop('_low_conf_col', None)
                    it['_verify_fixed'] = True  # GUI 可据此提示"经二次识别补全"
        out.append(it)
    return out






def ocr_table_row_split(image_path: str, columns: list, table_bbox: dict = None,
                        row_bboxes: list = None, group_size: int = 6,
                        forced_model: str = None) -> dict:
    """⚠ 行切分（v1.4.5 bug hunt L19 标注）：方案A（整表无 bbox）已拍板废弃行边界切分——
    大表 AI 行边界数错会切碎数据。本函数仅作为引用兼容残留，识别主路径绝不启用
    （DESIGN §1/§8： _use_split/_bd_rows 已删，row_bboxes 传 None）。勿再启用。

    遗留原实现注释：row_bboxes = [(top, bottom) 像素坐标, ...]（相对原图，含表头行）；
    返回 {'columns': columns, 'rows': [{列名: 值}, ...]}；任何一步失败抛
    RuntimeError（调用方 fallback 整表 ocr_table）。
    """
    from PIL import Image as PILImg
    import io as _io
    # columns=None → 全列模式（回归设计初衷：模型识别全表所有列，程序端按
    # mapping/勾选列筛选）。⚠ 不能解析成 selected——把勾选列清单丢给模型当
    # JSON key，勾选列漏配（如仓库信息）就丢列，列名交给模型对齐就串列
    # （v1.4 回归修复，与 ocr_table 的 columns=None 语义保持一致）
    _known = []  # 已知列名集合（表头行过滤用）：指定列=columns；全列=探测列/all
    if columns:
        _known = [str(c) for c in columns]
    else:
        try:
            from utils import get_ocr_columns
            _cc = get_ocr_columns()
            _known = [c for c in (_cc.get('all') or []) if c] or [c for c in (_cc.get('selected') or []) if c]
        except Exception:
            _known = []
    if not row_bboxes:
        raise RuntimeError('row_split 缺少 row_bboxes')
    # ── 模型输出上限自适应分组（v1.4 修复：全列模式下数量莫名变 2）──
    # 每行 9 列全列 JSON ≈ 450 token（实测商品名 30+ 字 + ID + 仓库地址等长文本，
    # 一行 400~500 token，旧估算 300 偏低导致 qwen-omni 2048 上限下 4 行即截断）；
    # glm-4v-flash 输出上限 1024 token（API 硬限制，_ocr_api_call_do 钳制）。
    # 按 上限/每行估算 动态缩小组；下方解析处另有"截断拆半重试"兜底（不依赖估算）。
    try:
        from utils import get_api_config as _gac2
        _acfg2 = _gac2()
        _ap2 = _acfg2.get('active_provider', '')
        _am2 = ((_acfg2.get('providers') or {}).get(_ap2, {}) or {}).get('model', '') or ''
        _fm2 = forced_model or ''
    except Exception:
        _am2 = _fm2 = ''
    _is_ocr = bool(_is_qwen_ocr(forced_model) or _is_qwen_ocr(_am2))
    _is_flash = any(
        (m or '').strip().lower().startswith('glm-4v-flash') or (m or '').strip().lower() == 'glm-4v-flash'
        for m in (_am2, _fm2))
    try:
        _per_row_tok = 450 if not columns else 140 + 40 * len(columns)
    except Exception:
        _per_row_tok = 450
    _cap_tok = 1024 if _is_flash else (4096 if _is_ocr else 2048)
    # 下限 2：组大小为 1 时下方"避免孤行组"的分组逻辑会把首行拆成孤组
    # （行重复/漏行风险），任何模型都不允许单行组（v1.4 审查加固）
    _group_size = max(2, min(group_size, _cap_tok // (_per_row_tok + 60)))
    _img = PILImg.open(image_path).convert('RGB')
    _W, _H = _img.size
    _l, _t, _r, _b = 0, 0, _W, _H
    if isinstance(table_bbox, dict):
        _l = int(table_bbox.get('left', 0)); _t = int(table_bbox.get('top', 0))
        _r = int(table_bbox.get('right', _W)); _b = int(table_bbox.get('bottom', _H))
    # row_bboxes 相对原图 → 相对表格图（v1.4 修复：行只需在原图内合法即可，
    # 不被 bbox 底部截断——AI 对 3 行小表格的 bbox 可能画矮，行边界超出 bbox
    # 会被旧逻辑过滤 → 静默丢最后一行）
    _rows = []
    for (_rt, _rb) in (row_bboxes or []):
        _rt2 = max(0, int(_rt) - _t)
        _rb2 = min(_H, int(_rb)) - _t
        if 0 <= _rt2 < _rb2 <= (_H - _t):
            _rows.append((_rt2, _rb2))
    if len(_rows) < 2:
        raise RuntimeError('row_bboxes 无效')
    # 行边界完整性校验：bbox 底部比最后一行底多出 >2.5 行高 → 行边界疑似
    # 漏了底部行（AI 数行不全），行切分必然丢行 → 抛错回退整表识别
    # （整表模型自己数行，与实时截图同路径；v1.4 修复。
    #  ⚠ 阈值 1.5→2.5 行：AI 的 bbox 常比表格实际范围画大 1~2 行，
    #  过小阈值会把正常表格误判漏行，导致行切分机制失效）
    try:
        _avg_h = (_rows[-1][1] - _rows[0][0]) / max(1, len(_rows))
        # 行边界异常（重叠/零高）时 _avg_h≈0，任何差值都会误触发——跳过校验
        if _avg_h >= 1 and (_b - _t) - _rows[-1][1] > 2.5 * _avg_h:
            raise RuntimeError('行边界未覆盖表格底部（疑似漏行），回退整表')
    except RuntimeError:
        raise
    except Exception:
        pass
    # 表格图裁剪：宽度聚焦 bbox；高度扩展到覆盖全部行边界（bbox 矮时不截行）
    _tbl_img = _img.crop((_l, _t, _r, min(_H, _t + max(_rows[-1][1], _b - _t))))
    # 分组：避免"孤行组"——最后一组只剩 1 行时（5 行拆 4+1），单行图
    # 无表头参照、图太矮，模型输出列不全（v1.4 修复：山东第4行数据不全）。
    # 拆组规则：前组剩余必须 ≥2 行；前组只有 2 行时直接并入孤行（3 行组，
    # 容忍超组大小 1 行——超量 token 由"截断拆半重试"兜底；v1.4 审查加固：
    # 组 2 场景下旧逻辑会拆出 [1,2] 首行孤组）
    _groups = []
    _i = 0
    while _i < len(_rows):
        _take = min(_group_size, len(_rows) - _i)
        if _take == 1 and _groups:
            _prev = _groups.pop()
            if len(_prev) >= 3:
                _groups.append(_prev[:-1])
                _groups.append([_prev[-1], _rows[_i]])
            else:
                _groups.append(_prev + [_rows[_i]])
            _i += 1
            continue
        _groups.append(_rows[_i:_i + _take])
        _i += _take
    _cols_txt = '、'.join(str(c) for c in columns) if columns else ''
    _ex_col = columns[0] if columns else '商品信息'
    all_rows = []
    def _recognize_group(_grp, _gi):
        """识别一组（递归）：调用模型 → 解析 JSON。
        解析失败（输出截断/非 JSON）且组可拆 → 拆半递归重试（v1.4 兜底，
        不依赖 token 估算——行长超预期时自动缩小粒度直到能完整解析）。
        单行仍失败 → 抛 RuntimeError（调用方回退整表识别）。"""
        _gt = _grp[0][0]; _gb = _grp[-1][1]
        _gimg = _tbl_img.crop((0, _gt, _tbl_img.width, _gb))
        _w2, _h2 = _gimg.size
        _r2 = 1280.0 / max(_w2, _h2)
        if _r2 < 1:
            _gimg = _gimg.resize((int(_w2 * _r2), int(_h2 * _r2)), PILImg.LANCZOS)
        # v1.4.2 组图增强（与整表同链路）：自适应对比度 + 锐化 + q95——
        # 行切分数字丢位（1234→123）主因是低对比度+有损压缩糊掉末位
        try:
            from PIL import ImageOps as _ImgOps, ImageFilter as _ImgFilter
            _gimg = _ImgOps.autocontrast(_gimg, cutoff=1)
            _gimg = _gimg.filter(_ImgFilter.UnsharpMask(radius=1.5, percent=120, threshold=2))
        except Exception:
            pass
        _buf = _io.BytesIO()
        _gimg.save(_buf, format='JPEG', quality=95)
        _b64 = base64.b64encode(_buf.getvalue()).decode()
        # Qwen OCR 专用模型：行切分每组小图，同样吃满 4096 token（输出更长更稳）
        # （_is_ocr 已在函数开头按 forced_model + 当前 provider 判定）
        _row_max_tok = 4096 if _is_ocr else 2048
        if columns:
            _prompt = f"""你是数据录入员，识别图中 PDD 后台表格的一个片段（共 {len(_grp)} 行，含表头）。
只识别以下列（严格按这些列名作为 JSON key，列名原样）：{_cols_txt}
输出严格 JSON：{{"rows": [{{"{_ex_col}": "值", ...}}, ...]}}
要求：
1. 按图中从上到下顺序逐行输出，一行不漏、不重复、不合并
2. 每行 key 用上面列名原样；缺某列的值填 null，**不要编造图中没有的内容**
3. 单元格值原样抄写，不要转换数字、不要去掉单位；数字后的日期时间不抄（如"258份 08-02"只抄"258份"）；**数字必须完整输出——末位 0 必须保留（如 1230 不能写成 123）、禁止丢位/截断/省略（v1.4.2 数字完整性强化）**
4. 只输出 JSON，不要解释"""
        else:
            # 全列模式：模型识别所有列，key 用表头列名原样（程序端按 mapping 筛选）
            _prompt = f"""你是数据录入员，识别图中 PDD 后台表格的一个片段（共 {len(_grp)} 行，含表头）。
识别每一行的**所有列**：JSON key 用图中表头列名原样（如"商品信息""仓库总库存""仓库预估总销售数""仓库信息"）。
输出严格 JSON：{{"rows": [{{"商品信息": "值", "仓库总库存": "值", ...}}, ...]}}
要求：
1. 按图中从上到下顺序逐行输出，一行不漏、不重复、不合并
2. 每行 key 用表头列名原样；某列缺值填 null，**不要编造图中没有的内容**
3. 单元格值原样抄写，不要转换数字、不要去掉单位；数字后的日期时间不抄（如"258份 08-02"只抄"258份"）；**数字必须完整输出——末位 0 必须保留（如 1230 不能写成 123）、禁止丢位/截断/省略（v1.4.2 数字完整性强化）**
4. 只输出 JSON，不要解释"""
        try:
            content, _ = _ocr_api_call(_b64, _prompt, max_tok=_row_max_tok, forced_model=forced_model)
        except Exception as e:
            raise RuntimeError(f'行组{_gi + 1}识别失败: {e}')
        _text = content.strip()
        if '```' in _text:
            for _p in _text.split('```'):
                _p = _p.strip()
                if _p.startswith('json'):
                    _p = _p[4:].strip()
                if _p.startswith('{') or _p.startswith('['):
                    _text = _p
                    break
        _ok = False
        if _text.startswith('{'):
            try:
                _data = json.loads(_text)
                _ok = True
            except (json.JSONDecodeError, ValueError):
                _ok = False
        if not _ok:
            # 截断/非 JSON：组可拆则拆半递归，单行仍失败才抛出
            if len(_grp) >= 2:
                _mid = len(_grp) // 2
                _recognize_group(_grp[:_mid], _gi)
                _recognize_group(_grp[_mid:], _gi)
                return
            raise RuntimeError(f'行组{_gi + 1}输出截断/非JSON无法解析')
        _rws = _data.get('rows') or _data.get('items') or []
        for _r in _rws:
            if isinstance(_r, dict) and _r:
                # 程序端过滤表头行：prompt 明确"含表头"，模型可能把表头
                # （商品信息|仓库总库存|…）当数据行输出——表头行的值恰好是
                # 列名本身，name 非空会被 parse_items_generic 录成幽灵商品
                # （v1.4 审查修复；全列模式下用已知列名集合判断）
                _first_val = str(next(iter(_r.values()), '') or '').strip()
                if _known and _first_val in _known:
                    continue
                # 兜底（不依赖 _known）：表头行的每个值都等于自己的 key
                # （{"商品信息": "商品信息", ...}）——客户未探测列时 _known 为空，
                # 只靠 _known 会漏过滤（v1.4 审查加固）
                if _r and all(str(v).strip() == str(k).strip() for k, v in _r.items()):
                    continue
                all_rows.append(_r)

    for _gi, _grp in enumerate(_groups):
        _recognize_group(_grp, _gi)
    return {'columns': columns, 'rows': all_rows}


def ocr_dual_verify_generic(image_path: str, columns: list = None, mapping: dict = None,
                            table_bbox: dict = None, secondary_model: str = 'glm-4v-flash',
                            row_bboxes: list = None) -> list:
    """
    双模型交叉验证（v1.3 通用列版）：主模型 + 副模型双路通用识别，
    按 name 匹配比较 stock/sales，差异 >30% 标记 _low_confidence=True，
    stock/sales 取保守较大值。返回主模型结果（含 _raw 与 _low_confidence 标记）。
    副模型失败回退主模型结果。
    row_bboxes（v1.4）：表格行级边界，传入时主/副模型优先行切分（防整表乱编），
    行切分失败自动回退整表——与单模型路径一致。
    """
    from utils import get_ocr_columns
    cfg = get_ocr_columns() if mapping is None else {'mapping': mapping, 'selected': columns}
    mapping = cfg.get('mapping') or {}
    # 单模型识别：行切分优先，失败回退整表（v1.4 与单模型路径一致）。
    # 全列模式（columns=None 时）：行切分与整表都识别所有列（columns=None 传下去，
    # row_split 内部全列模式），程序端后续按 mapping 筛选——
    # ⚠ 不能传 sel：把列清单丢给模型会丢列/串列（v1.4 回归修复）。
    # 主 / 副 / OCR跳过 三条路径共用这一个实现（v1.4.3 重构去重）。
    def _one(forced_model=None):
        if row_bboxes:
            try:
                _r = ocr_table_row_split(image_path, columns=None, table_bbox=table_bbox,
                                         row_bboxes=row_bboxes, forced_model=forced_model)
                return parse_items_generic(_r.get('rows') or [], mapping)
            except Exception:
                pass  # 行切分失败回退整表
        result = ocr_table(image_path, columns=None, table_bbox=table_bbox,
                           forced_model=forced_model)
        return parse_items_generic(result.get('rows') or [], mapping)
    # v1.4.2：副模型是 OCR 专用模型（qwen*-ocr）时直接跳过双模型表格验证——
    # OCR 专用模型输出的是「文字块列表」（{"行号","标题","rotate_rect","text"}），
    # 不是表格结构化 JSON（columns/rows），做对比必失败+白耗一次 API（客户实测：
    # 换了 VL 主模型后副模型 qwen3.5-ocr 每轮都报'无法解析 JSON'然后降级）。
    if secondary_model and _is_qwen_ocr(secondary_model):
        _ocr_dlog(f"副模型({secondary_model})为OCR专用模型，不参与表格JSON交叉验证——本次按单模型(主)识别")
        primary = _one(forced_model=None)
        if primary:
            for _it in primary:
                _it['_dual_degraded'] = True  # GUI 提示双模型未生效
        return primary

    # 主模型
    try:
        primary = _one(forced_model=None)
    except Exception:
        raise  # 主模型失败直接抛（与单模型路径一致，调用方会提示）

    if not primary:
        return primary

    # 副模型（失败回退主模型结果，但如实提示）
    # v1.4.2：降级提示限流——副模型配置错误（如 ep 缺失）时每次 OCR 都刷屏，
    # 5 分钟内同一原因只提示一次（日志仍完整，_dual_degraded 标记不丢）
    import time as _t_lmt
    _now = _t_lmt.time()
    try:
        _lmt_ok = _now - ocr_dual_verify_generic._last_degrade_ts > 300
    except Exception:
        _lmt_ok = True
    ocr_dual_verify_generic._last_degrade_ts = _now
    try:
        secondary = _one(forced_model=secondary_model)
    except Exception as e:
        if _lmt_ok:
            _ocr_dlog(f"⚠ 副模型({secondary_model})识别失败，已用主模型结果：{str(e)[:120]}")
        for _it in primary:
            _it['_dual_degraded'] = True  # GUI 据此提示用户双模型未生效
        return primary
    if not secondary:
        if _lmt_ok:
            _ocr_dlog(f"⚠ 副模型({secondary_model})无有效结果，已用主模型结果")
        for _it in primary:
            _it['_dual_degraded'] = True  # GUI 据此提示用户双模型未生效
        return primary

    def _norm(n):
        return str(n).replace(' ', '').lower()
    sec_by_name = {}
    sec_by_sku = {}
    for it in secondary:
        # 优先按 sku_id 建索引：同名不同规格（可口可乐330ml 两个不同 SKU）
        # 只按 name 会互相覆盖丢数据（v1.4 审查修复）
        _sku = _norm(it.get('sku_id'))
        _nm = _norm(it.get('name'))
        if _sku and _sku not in sec_by_sku:
            sec_by_sku[_sku] = it
        if _nm and _nm not in sec_by_name:
            sec_by_name[_nm] = it

    for item in primary:
        pn = _norm(item.get('name'))
        ps = _norm(item.get('sku_id'))
        match = None
        # 1) SKU 精确配对（权威锚点，同名不同规格不会错配）
        if ps and ps in sec_by_sku:
            match = sec_by_sku[ps]
        # 2) name 精确配对（无 SKU 或 SKU 未对上时）
        if not match and pn:
            match = sec_by_name.get(pn)
        # 3) name 编辑距离≤2 近似配对（OCR 单字误识别），标记低置信
        if not match and pn:
            for skey, sit in sec_by_name.items():
                if abs(len(skey) - len(pn)) <= 2 and _lev(skey, pn) <= 2:
                    match = sit
                    break
        if not match:
            # 主副模型都没配上：不静默保留串名——标记低置信，用户复核时能看到 ⚠
            # （v1.4 修复：此前 continue 直接输出主模型串名，数据串行无提示）
            item['_low_confidence'] = True
            item['_name_unmatched'] = True
            continue
        if match and _norm(match.get('name')) != pn:
            item['_low_confidence'] = True
        for field in ('stock', 'sales'):
            a, b = item.get(field, 0), match.get(field, 0)
            # 方向对称：分母取 max(a,b)（原 max(b,1) 在 a=100/b=10 时 900%，
            # a=10/b=100 时 90%，方向不对称；v1.4 审查修复）
            denom = max(a, b, 1)
            if abs(a - b) / denom > 0.3:
                item['_low_confidence'] = True
                # 不自动取大/小值：差异可能是漏识别（取大对）也可能是多识别
                # （真 110 被识别成 1109，取大错），无法区分 → 保持主模型值，标 ⚠ 让用户复核
    return primary
