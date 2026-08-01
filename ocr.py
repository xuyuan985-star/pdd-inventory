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
        _img = PILImg.open(image_path)
        if _img.mode != 'RGB':
            _img = _img.convert('RGB')
        _w, _h = _img.size
        _r = max_side / max(_w, _h)
        if _r < 1:
            _img = _img.resize((int(_w * _r), int(_h * _r)), PILImg.LANCZOS)
        _buf = _io.BytesIO()
        _img.save(_buf, format='JPEG', quality=quality)
        return base64.b64encode(_buf.getvalue()).decode()
    except Exception:
        # 预处理失败则回退原图
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode()


def _clean_json(text: str) -> str:
    """从OCR回复中提取纯JSON"""
    text = text.strip()
    # 去掉 markdown 代码块
    if '```' in text:
        parts = text.split('```')
        for p in parts:
            p = p.strip()
            if p.startswith('json'):
                p = p[4:].strip()
            if p.startswith('['):
                return p
    # 找第一个 [ 到最后一个 ]
    start = text.find('[')
    end = text.rfind(']')
    if start >= 0 and end > start:
        return text[start:end+1]
    return text


# 全角→半角映射常量（模块级缓存，避免 _parse_num_text 每次调用重建）
_FULLWIDTH_TRANS = str.maketrans('０１２３４５６７８９．', '0123456789.')


def _parse_num_text(v) -> int:
    """
    从单元格原始文字解析整数。
    "100份 查看"→100, "1,234"→1234, "1.5万"→15000, "统计中"→0
    返回 int；解析失败返回 0。
    """
    import re
    s = str(v).strip()
    if not s or s.lower() in ('none', 'null', 'nan', '-', '--', '/', '统计中', '查看', '暂无', '无'):
        return 0
    # 全角数字 → 半角
    s = s.translate(_FULLWIDTH_TRANS)
    # 去千分位
    s = s.replace(',', '').replace('，', '')
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    if not m:
        return 0
    num = float(m.group())
    # 单位换算
    if '万' in s:
        num *= 10000
    elif '千' in s:
        num *= 1000
    elif '亿' in s:
        num *= 100000000
    return int(round(num))


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
                        'index': item.get('index')})
    
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
    
    # 检查3：商品名过短（<3字）或全是数字/符号 → 幻觉
    valid_names = 0
    for it in cleaned:
        name = it['name']
        chinese_chars = sum(1 for c in name if '\u4e00' <= c <= '\u9fff')
        if chinese_chars >= 2 and len(name) >= 3:
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
   {"index": 行号从1开始, "name": "商品名", "stock_text": "库存单元格原始文字", "sales_text": "销量单元格原始文字", "region": "省份名或null"}
3. stock_text / sales_text 必须原样抄写单元格里的全部文字（如 "100份 查看"、"1,234"），不要自己转换数字、不要去掉单位
4. 无法识别的单元格填 null，不要编造
5. 整张截图没有订货表格、无有效商品数据时只输出 []
6. 只输出 JSON 数组，不要任何解释文字"""
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
