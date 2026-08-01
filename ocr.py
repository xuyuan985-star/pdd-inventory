"""
PDD 后台截图 OCR 识别
输入：PDD订货管理页面截图
输出：[{name, stock, sales}, ...]
"""

import base64, json, os, sys

import requests
from utils import get_api_config, get_base_dir


def _prep_image_b64(image_path: str, max_side: int = 1600, quality: int = 85) -> str:
    """
    统一图片预处理：长边缩放 + JPEG 压缩 → base64。
    所有提供商/模型共用，避免 doubao 压 1280 而 qwen/glm 直传原图的不一致。
    """
    try:
        from PIL import Image as PILImg
        import io as _io
        # 自适应表格裁剪：检测到表格区域则用裁剪图（更纯净、数字更大），失败回退原图
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


def _clean_json(text: str) -> str:
    """从OCR回复中提取纯JSON（数组或对象）"""
    text = text.strip()
    # 去掉 markdown 代码块
    if '```' in text:
        parts = text.split('```')
        for p in parts:
            p = p.strip()
            if p.startswith('json'):
                p = p[4:].strip()
            if p.startswith('[') or p.startswith('{'):
                return p
    # 找第一个 [ 到最后一个 ]（数组）
    start = text.find('[')
    end = text.rfind(']')
    if start >= 0 and end > start:
        return text[start:end+1]
    # 兜底：找第一个 { 到最后一个 }（对象，如 {"items":[...]}）
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        return text[start:end+1]
    return text


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
        y_top = max(0, int(best[0]) - 10)
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


def align_columns(items: list) -> list:
    """
    列对齐后处理：用模型返回的 stock_x/sales_x（相对整图宽度比例）校验列错位。
    同一列（如 stock 列）所有行的 x 比例应接近（方差小）；若某行 x 明显偏离
    该列中位数，且与另一列更接近，则判定为列错位并交换该行的 stock/sales。
    模型未返回 x 坐标（全部为 None）时原样返回。
    """
    if not items:
        return items
    stock_xs = [it.get('stock_x') for it in items]
    sales_xs = [it.get('sales_x') for it in items]
    if not any(x is not None for x in stock_xs + sales_xs):
        return items  # 无坐标信息，跳过

    def _median(vals):
        vs = sorted(v for v in vals if v is not None)
        if not vs:
            return None
        n = len(vs)
        return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2

    med_stock = _median(stock_xs)
    med_sales = _median(sales_xs)
    if med_stock is None or med_sales is None:
        return items

    for it in items:
        sx = it.get('stock_x')
        lx = it.get('sales_x')
        if sx is None or lx is None:
            continue
        # 若该行 stock_x 更接近 sales 列中位数、且 sales_x 更接近 stock 列中位数 → 列错位，交换
        if (abs(sx - med_sales) < abs(sx - med_stock)
                and abs(lx - med_stock) < abs(lx - med_sales)):
            it['stock'], it['sales'] = it['sales'], it['stock']
    return items


def _validate_items(items: list) -> list:
    """验证并修正OCR结果。兼容两种格式：
    新格式 {"name","stock_text","sales_text","region","index"}
    旧格式 {"name","stock","sales","region"}
    """
    # 防御：API 偶尔返回 {"items":[...]} 之类的 dict 结构
    if isinstance(items, dict):
        for k in ('items', 'data', 'results', 'list'):
            if isinstance(items.get(k), list):
                items = items[k]
                break
        else:
            return []
    if not isinstance(items, list):
        return []
    cleaned = []
    seen_names = set()  # 去重：同名商品只保留第一条
    for item in items:
        # 防御：模型返回 null 时 Python 解析为 None，转 str 会变成 "None"
        name = item.get('name')
        name = '' if name is None or str(name).strip().lower() in ('none', 'null', '') else str(name).strip()
        if not name:
            continue
        # 数字：优先解析原始文字（新格式），回退旧格式数字字段
        stock = _parse_num_text(item.get('stock_text', item.get('stock', 0)))
        sales = _parse_num_text(item.get('sales_text', item.get('sales', 0)))
        region = item.get('region')
        region = '' if region is None or str(region).strip().lower() in ('none', 'null', '') else str(region).strip()
        # 去重：完全同名
        if name in seen_names:
            continue
        seen_names.add(name)
        # 数值合理性：超过 99999999 视为读错位数，置 0
        if stock > 99999999 or stock < 0:
            stock = 0
        if sales > 99999999 or sales < 0:
            sales = 0
        cleaned.append({'name': name, 'stock': stock, 'sales': sales, 'region': region,
                        'index': item.get('index'),
                        'stock_x': item.get('stock_x'), 'sales_x': item.get('sales_x')})
    
    if not cleaned:
        return []
    
    # 按模型给的 index 恢复表格顺序（防御模型乱序输出；兼容字符串行号）
    def _index_key(it):
        try:
            return int(it.get('index'))
        except (TypeError, ValueError):
            return 99999
    if any(_index_key(it) != 99999 for it in cleaned):
        cleaned.sort(key=_index_key)
    
    # ── 幻觉数据过滤器 ──
    KNOWN_REGIONS = {'云南','广东','浙江','北京','上海','江苏','山东','四川','湖北','湖南','河南','河北',
                     '福建','安徽','辽宁','陕西','重庆','江西','广西','贵州','山西','吉林','黑龙江',
                     '甘肃','内蒙古','新疆','海南','宁夏','青海','西藏','天津','香港','澳门','台湾'}
    
    # 检查1：所有地区都是假地名 → 幻觉（宽容省/市后缀）
    def _strip_region(r):
        for sfx in ['特别行政区', '自治区', '省', '市']:
            if r.endswith(sfx):
                return r[:-len(sfx)]
        return r
    regions_found = {_strip_region(it['region']) for it in cleaned if it['region']}
    if regions_found and not regions_found & KNOWN_REGIONS:
        return []
    
    # 检查2 已移除：同名复读由 seen_names 去重拦截；
    # 「不同名但 stock/sales 全同」是同一系列 SKU 的真实场景，不再误杀。
    
    # 检查3：商品名过短（<2字）或全是数字/符号 → 幻觉
    # （2 字中文商品名、纯英文 SKU 如 "iPhone 15 Pro" 都是合法业务数据）
    valid_names = 0
    for it in cleaned:
        name = it['name']
        chinese_chars = sum(1 for c in name if '\u4e00' <= c <= '\u9fff')
        has_alpha = any(c.isalpha() for c in name)
        if len(name) >= 2 and (chinese_chars >= 1 or has_alpha):
            valid_names += 1
    if valid_names == 0 and len(cleaned) > 0:
        return []
    
    # 读错列兜底：仅当库存大到不可能是真实业务数据（≥10万且超销量1000倍）才置0。
    # 滞销品/预售品/季节性囤货的库存远大于销量是真实场景，不再静默清零。
    for it in cleaned:
        s, sa = it.get('stock', 0), it.get('sales', 0)
        if sa > 0 and s > sa * 1000 and s >= 100000:
            it['stock'] = 0
    
    # 清理内部字段，保持下游接口 {name, stock, sales, region}
    for it in cleaned:
        it.pop('index', None)
    
    # 列对齐校验：用 stock_x/sales_x 检测并修正列错位（模型未返回坐标时原样返回）
    cleaned = align_columns(cleaned)
    
    # 清理 x 坐标字段，保持下游接口纯净
    for it in cleaned:
        it.pop('stock_x', None)
        it.pop('sales_x', None)
    
    return cleaned


def ocr_screenshot(image_path: str, forced_model: str = None) -> list:
    """
    识别 PDD 后台截图。根据 settings 中提供商配置选择 API。
    """
    img_b64 = _prep_image_b64(image_path)

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
            # 模型名优先；custom_endpoint（ep-xxx 推理接入点）仅当未填模型名时兜底，
            # 避免过期的接入点 ID 顶掉用户配置的模型名
            custom_ep = provider.get('custom_endpoint', '')
            fallback = model_name or custom_ep
            models = [m for m in [fallback, 'glm-4v-flash'] if m and m.strip()]
        else:
            models = [m for m in [model_name, 'glm-4v-flash'] if m and m.strip()]
    elif active == 'qwen':
        if not endpoint:
            endpoint = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        models = [m for m in [model_name, 'glm-4v-flash'] if m and m.strip()] if model_name else ['qwen3.5-omni-flash', 'glm-4v-flash']
    else:  # glm
        if not endpoint:
            endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        models = [m for m in [model_name, 'glm-4v-flash'] if m and m.strip()] if model_name else ['glm-4v-flash']

    # 统一提示词 — 抄写原文，不做语义转换；index 锚定行顺序
    prompt = """你是数据录入员，识别图中 PDD 后台订货表格。表格为竖向列表，每行一个商品。

版面说明：
- 表头行包含：商品名称、仓库总库存、仓库预估总销售数、省份（部分截图无省份列）
- 商品名称：文字较长的一列，原样抄写
- 仓库总库存：单元格文字形如「100份 查看」，数字后带「份」和「查看」链接
- 仓库预估总销售数：纯数字列，可能带单位「份」或千分位，如 1234 或 1,234
- 省份：有省份列则抄写省份名（如 山东），无省份列填 null

输出要求：
1. 严格按表格从上到下的顺序，逐行输出，一行不漏、不重复、不合并
2. 每行输出一个 JSON 对象：
   {"index": 行号从1开始, "name": "商品名", "stock_text": "库存单元格原始文字", "sales_text": "销量单元格原始文字", "region": "省份名或null", "stock_x": 库存数字中心相对整图宽度的比例, "sales_x": 销量数字中心相对整图宽度的比例}
3. stock_text / sales_text 必须原样抄写单元格里的全部文字（如 "100份 查看"、"1,234"），不要自己转换数字、不要去掉单位
4. stock_x / sales_x 是 0~1 之间的小数（如 0.62），表示该数字水平位置；无法确定时填 null
5. 无法识别的单元格填 null，不要编造
6. 整张截图没有订货表格、无有效商品数据时只输出 []
7. 只输出 JSON 数组，不要任何解释文字

示例（仅示意格式，不是真实数据）：
[{"index": 1, "name": "新疆灰枣500g", "stock_text": "128份 查看", "sales_text": "1,234", "region": "新疆", "stock_x": 0.62, "sales_x": 0.78},
 {"index": 2, "name": "云南普洱饼茶357g", "stock_text": "0份 查看", "sales_text": "0", "region": "云南", "stock_x": 0.62, "sales_x": 0.78}]"""
    max_tok = 1024

    for attempt, mdl in enumerate(models):
        # 如果fallback是智谱模型但当前走的是阿里/豆包端点，切换endpoint + 格式
        # 模型名/端点判断统一小写化，避免用户配 "GLM-4V-Flash"/"Responses" 时匹配失败
        cur_endpoint = endpoint
        cur_key = key
        cur_responses = use_responses
        mdl_l = mdl.lower()
        ep_l = cur_endpoint.lower()
        # 精确/前缀匹配智谱模型名，避免自定义模型名含 "glm" 子串被误判
        is_glm = mdl_l.startswith('glm-') or mdl_l == 'glm'
        if is_glm and 'dashscope' in ep_l:
            cur_endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
            cur_key = providers.get('glm', {}).get('api_key', '') if isinstance(providers, dict) else ''
            if not cur_key:
                continue
            cur_responses = False
        elif is_glm and ('ark' in ep_l or 'responses' in ep_l):
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
                        'max_output_tokens': max_tok,
                        'stream': False
                    }, timeout=60)
                data = resp.json()
                if 'output' not in data:
                    if attempt == 0:
                        continue
                    raise RuntimeError(f"OCR失败: {data}")
                # output[-1] = 最后一条消息（跳过 reasoning）
                content = data['output'][-1]['content'][0]['text']
            else:
                # Chat Completions 分支：GLM API 不识别 thinking 参数，仅非智谱模型发送
                cc_payload = {
                    'model': mdl,
                    'messages': [{'role': 'user', 'content': [
                        {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
                        {'type': 'text', 'text': prompt}
                    ]}],
                    'temperature': 0.0, 'max_tokens': max_tok,
                }
                if not is_glm:
                    cc_payload['thinking'] = {'type': 'disabled'}
                resp = requests.post(cur_endpoint,
                        headers={'Authorization': f'Bearer {cur_key}', 'Content-Type': 'application/json'},
                    json=cc_payload, timeout=60)
                data = resp.json()
                if 'choices' not in data:
                    if attempt == 0:
                        continue
                    raise RuntimeError(f"OCR失败: {data}")
                content = data['choices'][0]['message']['content']
            clean = _clean_json(content)
            items = json.loads(clean)
            validated = _validate_items(items)
            # qwen3.5-omni-flash 等模型字段名兼容：映射到统一字段后重新校验
            if not validated and isinstance(items, list):
                for it in items:
                    if 'goods_name' in it and 'name' not in it:
                        it['name'] = it.get('goods_name', '')
                    if 'sales_volume' in it and 'sales_text' not in it:
                        it['sales_text'] = it.get('sales_volume', '')
                    if 'stock' in it and 'stock_text' not in it:
                        it['stock_text'] = it.get('stock', '')
                    if 'area' in it and 'region' not in it:
                        it['region'] = it.get('area', '')
                validated = _validate_items(items)
            
            if validated:
                return validated
            
            # If empty, retry with backup model
            if attempt == 0:
                continue
                
        except json.JSONDecodeError:
            if attempt == 0:
                continue
        except Exception as e:
            if attempt == 0:
                continue
            raise

    raise RuntimeError("无法从截图中提取有效数据，请确保截图中包含PDD订货管理表格")


def ocr_screenshot_crosscheck(image_path: str, forced_model: str = None) -> list:
    """单次 OCR 识别，底层 ocr_screenshot 内部已有 fallback 模型重试。"""
    return ocr_screenshot(image_path, forced_model)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("用法: python ocr.py 后台截图.jpg")
        sys.exit(1)

    items = ocr_screenshot(sys.argv[1])
    for item in items:
        print(f"{item['name']}: 库存={item['stock']}, 销量={item['sales']}")

    # Auto compute
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from main import calculate_replenishment, generate_schedule, export_results

    inventory = [{'sku': i['name'][:12], 'name': i['name'], 'stock': i['stock']} for i in items]
    sales = {i['name'][:12]: {'sales': i['sales']} for i in items}

    plans = calculate_replenishment(inventory, sales)
    schedule = generate_schedule(plans)
    path = export_results(plans, os.path.join(get_base_dir(), 'output'))
    print(f'\n导出: {path}')


def ocr_dual_verify(image_path: str, secondary_model: str = 'glm-4v-flash') -> list:
    """
    双模型交叉验证：主模型 + 副模型双路 OCR，按 name 匹配比较 stock/sales。
    对差异 >30% 的字段标记 _low_confidence=True（供 UI 标红提示），
    stock 保守取两模型较大值（库存宁可多看，避免漏补）。
    返回主模型结果（带 _low_confidence 标记），副模型失败时回退主模型结果。
    """
    primary = ocr_screenshot(image_path)
    if not primary:
        return primary

    try:
        secondary = ocr_screenshot(image_path, forced_model=secondary_model)
    except Exception:
        return primary  # 副模型失败（无 Key/网络），回退单模型结果

    if not secondary:
        return primary

    # 按 name 建立副模型索引（去空白归一化）
    def _norm(n):
        return str(n).replace(' ', '').lower()
    sec_by_name = {}
    for it in secondary:
        key = _norm(it.get('name'))
        if key and key not in sec_by_name:
            sec_by_name[key] = it

    for item in primary:
        match = sec_by_name.get(_norm(item.get('name')))
        if not match:
            continue
        # 字段差异 >30% → 标记待复核；stock/sales 均取保守较大值
        # （销量高估比低估更安全：宁可多补货，避免断货）
        for field in ('stock', 'sales'):
            a, b = item.get(field, 0), match.get(field, 0)
            denom = max(b, 1)
            if abs(a - b) / denom > 0.3:
                item['_low_confidence'] = True
                item[field] = max(a, b)
    return primary
