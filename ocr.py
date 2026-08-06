"""
PDD 后台截图 OCR 识别
输入：PDD订货管理页面截图
输出：[{name, stock, sales}, ...]
"""

import base64, json, os

import requests
from utils import get_api_config, get_base_dir


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


def _prep_image_b64(image_path: str, max_side: int = 1280, quality: int = 80,
                    table_bbox: dict = None) -> str:
    """
    统一图片预处理：长边缩放 + JPEG 压缩 → base64。
    裁剪优先级：AI 定位 bbox（table_bbox）→ OpenCV auto_crop_table → 原图。
    所有提供商/模型共用，避免 doubao 压 1280 而 qwen/glm 直传原图的不一致。
    """
    try:
        from PIL import Image as PILImg
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
        _w, _h = _img.size
        _r = max_side / max(_w, _h)
        if _r < 1:
            _img = _img.resize((int(_w * _r), int(_h * _r)), PILImg.LANCZOS)
        _buf = _io.BytesIO()
        _img.save(_buf, format='JPEG', quality=quality)
        return base64.b64encode(_buf.getvalue()).decode()
    except Exception as e:
        # 预处理失败则回退原图（记录警告便于排查识别率下降）
        print(f"[OCR] 图片预处理失败，回退原图: {e} ({image_path})")
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode()




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
        y_bottom = min(h, int(best[-1]) + 10)

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
                  forced_model: str = None) -> tuple:
    """
    通用视觉 API 调用：按 settings 提供商配置选端点/模型。
    识别失败如实抛出错误，不偷偷 fallback 到其他模型（v1.3：一切如实）。
    返回 (content_text, model_used)；失败抛 RuntimeError。
    """
    api_cfg = get_api_config()
    active = api_cfg.get('active_provider', 'doubao')
    providers = api_cfg.get('providers', {})
    provider = providers.get(active, {}) if isinstance(providers, dict) else {}

    key = provider.get('api_key', '') or os.environ.get(
        {'doubao':'ARK_API_KEY','qwen':'DASHSCOPE_API_KEY','glm':'ZHIPU_API_KEY'}.get(active, ''), '')
    model_name = forced_model or provider.get('model', '')
    endpoint = provider.get('endpoint', '')
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
        mdl = mdl.strip()  # 用户可能输入带前后空格的模型名，发送前清理
        # glm-4v-flash 输出上限 1024（与 vision._call_vision_api 一致）：超限会 400
        if mdl.lower().startswith('glm-4v-flash') or mdl.lower() == 'glm-4v-flash':
            cur_max_tok = min(max_tok, 1024)
        else:
            cur_max_tok = max_tok
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
            cur_key = providers.get('glm', {}).get('api_key', '') if isinstance(providers, dict) else ''
            if not cur_key:
                continue
            cur_responses = False
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
                    }, timeout=(10, 30))
                data = resp.json()
                if not data.get('output'):
                    raise RuntimeError(f"OCR失败（{mdl}）: {data}")
                # output[-1] = 最后一条消息（跳过 reasoning）
                content = data['output'][-1]['content'][0]['text']
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
                    json=cc_payload, timeout=(10, 30))
                data = resp.json()
                if not data.get('choices'):
                    raise RuntimeError(f"OCR失败（{mdl}）: {data}")
                content = data['choices'][0]['message']['content']
            return content, mdl
        except json.JSONDecodeError as e:
            raise RuntimeError(f"模型返回无法解析的内容（{mdl}）：{e}")
        except RuntimeError:
            raise
        except Exception as e:
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
        '烟台1仓查看地址' → '烟台1仓'，'烟台1仓 竞看地址' → '烟台1仓'（查→竞），
        '65份\\n08-04 15:36\\n更新记录' → '65份'，'128份 08-02' → '128份'，
        '查看地址' → ''；名称中非尾部的「查看」（如"查看库存"）不受影响。
    """
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
    return _re.sub(r'[ \t\u3000\r\n]+', ' ', s).strip()


def dedup_items(items, seen_sku, seen_name_no_sku, seen_name_with_id):
    """按 sku_id 权威去重（无 ID 回退 name），返回去重后的新条目；就地更新三个 seen 集合。

    滚动加载多轮截图时，同一商品会反复出现；商品名有 OCR 单字波动（结→丝），
    **sku_id 长数字串每轮也可能错 1~2 位**（实测 96622588033→966225988033、984387564986→284337564986），
    极端时整段错位（939262347672→984387564986，编辑距离远超 2）。所以：
      - ID 精确命中 → 同商品去重；
      - ID 近匹配（编辑距离≤2）且 name 前 6 字相同 + 总距离≤6 → 同商品（OCR 数字错位）；
      - **OR name 强相似（前 6 字相同 + 总距离≤6）→ 同商品**（sku 整段错位时靠 name 兜底；
        商品名比 sku 稳定——同商品核心名稳定、尾部描述词乱，相邻商品前缀通常不同不会误并）；
      - 无 ID → name 精确去重。
    覆盖场景：同名不同ID保留 / 先无ID后有ID拦截 / 先有ID后无ID拦截 / 无ID同名去重 / 同ID名字波动 /
    **同商品ID错位波动** / **同商品sku整段错位但name相似**。
    seen_sku 为 dict {sku_id: name}（name 佐证）。
    """
    out = []
    for it in items:
        nm = it.get('name', '')
        sku = it.get('sku_id', '')
        if not nm:
            continue
        if sku:
            if sku in seen_sku:
                continue
            # 模糊兜底：同商品三路判断——
            #  1. ID 精确命中 → 同商品（sku 权威锚点）
            #  2. ID 近匹配(≤2) 且 name 前6字相同 → 同商品（OCR 数字错位；相邻商品 ID 虽近但
            #     name 前缀不同，不误并——防"盐渍鞭炮笋/9662...033"vs"盐渍海带结/9662...034"）
            #  3. ID 不近 但 name 强相似（前6字相同 + 总距离≤6 且 name 非完全相同）→ 同商品
            #     （sku 整段错位时靠 name 兜底；name 完全相同 ≠ 同商品：同名不同规格必须保留）
            hit = False
            for _s, _n in seen_sku.items():
                _id_near = abs(len(_s) - len(sku)) <= 2 and _lev(_s, sku) <= 2
                _name_prefix = _n[:6] == nm[:6]
                if _id_near and _name_prefix:
                    hit = True
                    break
                if not _id_near and _n != nm and _name_prefix and _lev(_n, nm) <= 6:
                    hit = True
                    break
            if hit:
                continue
            seen_sku[sku] = nm
            if nm in seen_name_no_sku:
                continue
            seen_name_with_id.add(nm)
        else:
            if nm in seen_name_no_sku or nm in seen_name_with_id:
                continue
            seen_name_no_sku.add(nm)
        out.append(it)
    return out


def strip_warehouse_noise(warehouse: str) -> str:
    """仓库信息去词条噪音（兼容别名，走通用 strip_tail_noise）。

    例：'烟台1仓 查看地址' → '烟台1仓'，'烟台1仓\\n查看地址' → '烟台1仓'。
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


_SKU_ID_RE = None  # 延迟初始化（正则编译一次）


def _split_name_id(value: str) -> tuple:
    """
    拆分商品信息列的值 → (商品名, sku_id)。
    后台格式：'盐渍鞭炮笋500g/袋 ID:96622588033' → ('盐渍鞭炮笋500g/袋', '96622588033')
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
    out = {'name': '', 'stock': '', 'sales': '', 'region': '', 'warehouse': '',
           'sku_id': '', '_raw': dict(row)}
    for col, val in row.items():
        field = norm_map.get(normalize_col_name(col))
        if field and field in ('name', 'stock', 'sales', 'region', 'warehouse'):
            out[field] = '' if val is None else str(val)
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
            # 无商品 ID 直接过滤：实测真实行 100% 带 ID（如 '盐渍鞭炮笋500g/袋 ID:96622588033'），
            # 无 ID = 模型乱编名字 / 漏识别 ID（豆包常见），放行会污染结果表
            _ocr_dlog(f"⏭ 无商品ID已过滤: {name[:40]}")
            continue
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
              table_bbox: dict = None) -> dict:
    """
    通用表格 OCR：识别 PDD 后台表格的任意列（不再局限于固定商品字段）。

    columns=None → 探测模式：识别表头所有列 + 每行每列的值。
    columns=[...] → 指定列模式：只识别指定列（客户勾选的列）。

    返回 {'columns': [列名...], 'rows': [{列名: 单元格原文, ...}, ...]}
    失败/无数据抛 RuntimeError（调用方处理）。
    """
    img_b64 = _prep_image_b64(image_path, table_bbox=table_bbox)

    if columns:
        cols_txt = '、'.join(str(c) for c in columns)
        # 示例 JSON 先整体序列化（列名可能含引号/花括号，json.dumps 保证安全且不破坏 f-string 语法）
        _ex_col1 = columns[0]
        _ex_col2 = columns[1] if len(columns) > 1 else '库存'
        _example_json = json.dumps({_ex_col1: "盐渍海带苗500g", _ex_col2: "128份"},
                                   ensure_ascii=False)
        prompt = f"""你是数据录入员，识别图中 PDD 后台表格。表格为竖向列表，每行一条数据。

只识别以下列（严格按这些列名作为 JSON key，列名原样）：{cols_txt}

输出要求：
1. 严格按表格从上到下顺序逐行输出，一行不漏、不重复、不合并
2. 每行输出一个 JSON 对象，key 用上面给的列名原样（缺某列的值填 null，不要编造）
3. 单元格值原样抄写，不要转换数字、不要去掉单位；数字后的日期时间不抄（如"258份 08-02"只抄"258份"）。**数字类列（库存/销量）只抄第一个数值，数值后如果还有其他数字串/时间，一律不要抄**（如"102份 12345"只抄"102份"）。**如果数字看起来异常多位（如 1109、100000），要核对是否把其他文字/格式读进去了，数字通常是简洁的整数**
4. 值为 0 是真实业务数据，该行必须保留，绝不能跳过
5. 商品信息类列（如「商品信息」「商品名称」）包含商品名和商品ID（如"盐渍鞭炮笋500g/袋 ID:96622588033"），必须完整原样抄写，不得去掉 ID 部分——商品ID用于区分重名商品。商品名**逐字原样抄写**，禁止用形近字/同音字替换（如"结"写成"丝"、"己"写成"已"），看不清的宁可填 null。**ID 是纯数字串（ID: 后跟一串数字），必须逐位核对，数字识别不清时宁可省略 ID 也不要编造/改位**
6. 整张截图没有有效表格时只输出 []
7. 只输出 JSON 数组，不要任何解释文字

示例（仅示意格式）：
[{_example_json}]"""
        # 列多时按列数放大 token，但设 8192 上限防止过度消耗 API 额度
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
- 单元格值原样抄写，不要转换数字、不要去掉单位；数字后的日期时间不抄。**数字类列（库存/销量）只抄第一个数值，数值后如果还有其他数字串/时间，一律不要抄**（如"102份 12345"只抄"102份"）。**如果数字看起来异常多位（如 1109、100000），要核对是否把其他文字/格式读进去了，数字通常是简洁的整数**
- 商品信息类列（如「商品信息」）含商品名和商品ID（如"盐渍鞭炮笋500g/袋 ID:96622588033"），必须完整原样抄写，不得去掉 ID 部分。商品名**逐字原样抄写**，禁止用形近字/同音字替换（如"结"写成"丝"、"己"写成"已"），看不清的宁可填 null。**ID 是纯数字串，必须逐位核对，数字识别不清时宁可省略 ID 也不要编造/改位**
- 值为 0 是真实业务数据，必须保留该行
- 无法识别的单元格填 null，不要编造
- 表格为空或无有效数据时输出 {"columns": [], "rows": []}"""
        max_tok = 2048  # glm-4.6v 有 reasoning 需更大预算；glm-4v-flash 由 _ocr_api_call 自动钳制到 1024

    content, _mdl = _ocr_api_call(img_b64, prompt, max_tok=max_tok, forced_model=forced_model)

    # 解析：优先 {"columns","rows"} 结构，兜底纯数组
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
        _dbg_note = ''
        if text.startswith('{'):
            data = json.loads(text)
            cols = data.get('columns') or []
            rows = data.get('rows') or data.get('items') or []
            # rows 可能是数组格式 [["a","b"],...]（glm-4.6v 偶发），按 columns 对齐转 dict
            # 行短于列 → 补 None（符合"缺列填 null"语义）；行长于列 → 截断；非 list 行原样保留
            if cols and rows:
                _n = len(cols)
                def _norm_row(r):
                    if isinstance(r, list):
                        return dict(zip(cols, (r + [None] * _n)[:_n]))
                    return r
                rows = [_norm_row(r) for r in rows]
            # 表头/数据对齐校验：模型 columns 声明可能与行对象 key 不一致
            # （漏列/多列/错序/列名近似），行数据标注更接近视觉 → 以行 key 多数票为准
            dict_rows = [r for r in rows if isinstance(r, dict) and r]
            if dict_rows:
                from collections import Counter as _C
                _kc = _C(tuple(r.keys()) for r in dict_rows)
                _top_keys = list(_kc.most_common(1)[0][0])
                if set(cols) != set(_top_keys):
                    _dbg_note = f"columns_rebased: {cols} -> {_top_keys}"
                    cols = _top_keys
                elif list(cols) != _top_keys:
                    _dbg_note = f"columns_reordered: {cols} -> {_top_keys}"
                    cols = _top_keys
            _write_ocr_debug(cols, rows, _dbg_note)
            return {'columns': list(cols), 'rows': list(rows)}
        # 纯数组：从第一行推断列名（无表头信息，尽力而为）
        rows = json.loads(text)
        if rows and isinstance(rows[0], dict):
            cols = list(rows[0].keys())
            _write_ocr_debug(cols, rows)
            return {'columns': cols, 'rows': rows}
        _write_ocr_debug([], rows, 'no_columns')
        return {'columns': [], 'rows': []}
    except json.JSONDecodeError:
        raise RuntimeError("模型返回无法解析的 JSON")






def ocr_table_row_split(image_path: str, columns: list, table_bbox: dict = None,
                        row_bboxes: list = None, group_size: int = 6,
                        forced_model: str = None) -> dict:
    """
    行级切分识别（v1.4，借鉴 surya 行级 bbox 思路）：按 row_bboxes 把表格裁成
    多组小图，每组独立识别。组内行数少 → 模型只能抄图中内容，防整表乱编。

    row_bboxes = [(top, bottom) 像素坐标, ...]（相对原图，含表头行）。
    返回 {'columns': columns, 'rows': [{列名: 值}, ...]}；
    任何一步失败抛 RuntimeError（调用方 fallback 整表 ocr_table）。
    """
    from PIL import Image as PILImg
    import io as _io
    if not columns or not row_bboxes:
        raise RuntimeError('row_split 缺少 columns/row_bboxes')
    _img = PILImg.open(image_path).convert('RGB')
    _W, _H = _img.size
    _l, _t, _r, _b = 0, 0, _W, _H
    if isinstance(table_bbox, dict):
        _l = int(table_bbox.get('left', 0)); _t = int(table_bbox.get('top', 0))
        _r = int(table_bbox.get('right', _W)); _b = int(table_bbox.get('bottom', _H))
    _tbl_img = _img.crop((_l, _t, _r, _b))
    # row_bboxes 相对原图 → 相对表格图
    _rows = []
    for (_rt, _rb) in (row_bboxes or []):
        _rt2 = max(0, int(_rt) - _t)
        _rb2 = min(_b, int(_rb)) - _t
        if 0 <= _rt2 < _rb2 <= (_b - _t):
            _rows.append((_rt2, _rb2))
    if len(_rows) < 2:
        raise RuntimeError('row_bboxes 无效')
    _groups = [_rows[i:i + group_size] for i in range(0, len(_rows), group_size)]
    _cols_txt = '、'.join(str(c) for c in columns)
    _ex_col = columns[0]
    all_rows = []
    for _gi, _grp in enumerate(_groups):
        _gt = _grp[0][0]; _gb = _grp[-1][1]
        _gimg = _tbl_img.crop((0, _gt, _tbl_img.width, _gb))
        _w2, _h2 = _gimg.size
        _r2 = 1280.0 / max(_w2, _h2)
        if _r2 < 1:
            _gimg = _gimg.resize((int(_w2 * _r2), int(_h2 * _r2)), PILImg.LANCZOS)
        _buf = _io.BytesIO()
        _gimg.save(_buf, format='JPEG', quality=80)
        _b64 = base64.b64encode(_buf.getvalue()).decode()
        _prompt = f"""你是数据录入员，识别图中 PDD 后台表格的一个片段（共 {len(_grp)} 行，含表头）。
只识别以下列（严格按这些列名作为 JSON key，列名原样）：{_cols_txt}
输出严格 JSON：{{"rows": [{{"{_ex_col}": "值", ...}}, ...]}}
要求：
1. 按图中从上到下顺序逐行输出，一行不漏、不重复、不合并
2. 每行 key 用上面列名原样；缺某列的值填 null，**不要编造图中没有的内容**
3. 单元格值原样抄写，不要转换数字、不要去掉单位；数字后的日期时间不抄（如"258份 08-02"只抄"258份"）
4. 只输出 JSON，不要解释"""
        try:
            content, _ = _ocr_api_call(_b64, _prompt, max_tok=2048, forced_model=forced_model)
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
        if _text.startswith('{'):
            _data = json.loads(_text)
            _rws = _data.get('rows') or _data.get('items') or []
            for _r in _rws:
                if isinstance(_r, dict) and _r:
                    all_rows.append(_r)
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
    sel = [c for c in (columns or cfg.get('selected') or []) if c]
    if not sel:
        sel = ['商品信息', '仓库总库存', '仓库预估总销售数']

    def _one(forced_model=None):
        """单模型识别：行切分优先，失败回退整表（v1.4 与单模型路径一致）"""
        if row_bboxes:
            try:
                _r = ocr_table_row_split(image_path, columns=sel, table_bbox=table_bbox,
                                         row_bboxes=row_bboxes, forced_model=forced_model)
                return parse_items_generic(_r.get('rows') or [], mapping)
            except Exception:
                pass  # 行切分失败回退整表
        result = ocr_table(image_path, columns=sel, table_bbox=table_bbox,
                           forced_model=forced_model)
        return parse_items_generic(result.get('rows') or [], mapping)

    # 主模型
    try:
        primary = _one(forced_model=None)
    except Exception:
        raise  # 主模型失败直接抛（与单模型路径一致，调用方会提示）

    if not primary:
        return primary

    # 副模型（失败回退主模型结果，但如实提示）
    try:
        secondary = _one(forced_model=secondary_model)
    except Exception as e:
        _ocr_dlog(f"⚠ 副模型({secondary_model})识别失败，已用主模型结果：{str(e)[:120]}")
        for _it in primary:
            _it['_dual_degraded'] = True  # GUI 据此提示用户双模型未生效
        return primary
    if not secondary:
        _ocr_dlog(f"⚠ 副模型({secondary_model})无有效结果，已用主模型结果")
        for _it in primary:
            _it['_dual_degraded'] = True  # GUI 据此提示用户双模型未生效
        return primary

    def _norm(n):
        return str(n).replace(' ', '').lower()
    sec_by_name = {}
    for it in secondary:
        key = _norm(it.get('name'))
        if key and key not in sec_by_name:
            sec_by_name[key] = it

    for item in primary:
        pn = _norm(item.get('name'))
        match = sec_by_name.get(pn) if pn else None
        # 主模型 name 单字误识别（如 结→丝）时精确匹配不上：
        # 用编辑距离≤1 做近似配对，配对后标记低置信度提示用户复核
        if not match and pn:
            for skey, sit in sec_by_name.items():
                if abs(len(skey) - len(pn)) <= 1 and _lev(skey, pn) <= 1:
                    match = sit
                    break
        if not match:
            continue
        if match and _norm(match.get('name')) != pn:
            item['_low_confidence'] = True
        for field in ('stock', 'sales'):
            a, b = item.get(field, 0), match.get(field, 0)
            denom = max(b, 1)
            if abs(a - b) / denom > 0.3:
                item['_low_confidence'] = True
                # 不自动取大/小值：差异可能是漏识别（取大对）也可能是多识别
                # （真 110 被识别成 1109，取大错），无法区分 → 保持主模型值，标 ⚠ 让用户复核
    return primary
