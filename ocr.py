"""
PDD 后台截图 OCR 识别
输入：PDD订货管理页面截图
输出：[{name, stock, sales}, ...]
"""

import base64, json, os, sys, time

import requests
from utils import get_api_config, get_base_dir


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


def _validate_items(items: list) -> list:
    """验证并修正OCR结果"""
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
    for item in items:
        # 防御：模型返回 null 时 Python 解析为 None，转 str 会变成 "None"
        name = item.get('name')
        name = '' if name is None or str(name).strip().lower() in ('none', 'null', '') else str(name).strip()
        if not name:
            continue
        # 数字清洗：去单位（份、件、个等）
        def _clean_num(v):
            import re
            s = str(v).strip()
            m = re.search(r'[\d.]+', s)
            return float(m.group()) if m else 0.0
        try: stock = int(_clean_num(item.get('stock', 0)))
        except (ValueError, TypeError): stock = 0
        try: sales = int(_clean_num(item.get('sales', 0)))
        except (ValueError, TypeError): sales = 0
        region = item.get('region')
        region = '' if region is None or str(region).strip().lower() in ('none', 'null', '') else str(region).strip()
        cleaned.append({'name': name, 'stock': stock, 'sales': sales, 'region': region})
    
    if not cleaned:
        return []
    
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
    
    # 检查2：>4个商品 stock+sales 都相同且名称也相似 → 幻觉
    stocks = {it['stock'] for it in cleaned}
    sales_set = {it['sales'] for it in cleaned}
    if len(stocks) == 1 and len(sales_set) == 1 and len(cleaned) >= 5:
        # 检查商品名是否也高度相似
        names = [it['name'] for it in cleaned]
        sample = names[0]
        similar = sum(1 for n in names if n[:4] == sample[:4])
        if similar == len(names):
            return []
    
    # 检查3：商品名过短（<3字）或全是数字/符号 → 幻觉
    valid_names = 0
    for it in cleaned:
        name = it['name']
        chinese_chars = sum(1 for c in name if '\u4e00' <= c <= '\u9fff')
        if chinese_chars >= 2 and len(name) >= 3:
            valid_names += 1
    if valid_names == 0 and len(cleaned) > 0:
        return []
    
    return cleaned


def ocr_screenshot(image_path: str, model: str = 'glm-4v-flash') -> list:
    """
    识别 PDD 后台截图。根据 settings 中提供商配置选择 API。
    """
    with open(image_path, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()

    api_cfg = get_api_config()
    active = api_cfg.get('active_provider', 'doubao')
    providers = api_cfg.get('providers', {})
    provider = providers.get(active, {}) if isinstance(providers, dict) else {}

    key = provider.get('api_key', '') or os.environ.get(
        {'doubao':'ARK_API_KEY','qwen':'DASHSCOPE_API_KEY','glm':'ZHIPU_API_KEY'}.get(active, ''), '')
    model_name = provider.get('model', '')
    endpoint = provider.get('endpoint', '')
    use_responses = False

    if not key:
        raise RuntimeError(f"API Key 未设置 — 请在「API 管理」页面配置 {active} 的 Key")

    # 根据提供商确定默认值，但以用户设置的 endpoint 为准
    if active == 'doubao':
        if not endpoint:
            endpoint = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
        # 根据 endpoint 判断 API 类型，而非模型名
        use_responses = ('responses' in endpoint)
        if use_responses:
            models = [m for m in ['ep-20260710230432-jccpg', 'glm-4v-flash'] if m]
            # 图片预处理：1280px JPEG 压缩
            try:
                from PIL import Image as PILImg
                import io as _io
                _img = PILImg.open(image_path)
                _w, _h = _img.size
                _r = 1280 / max(_w, _h)
                if _r < 1:
                    _img = _img.resize((int(_w*_r), int(_h*_r)), PILImg.LANCZOS)
                _buf = _io.BytesIO()
                _img.save(_buf, format='JPEG', quality=75)
                img_b64 = base64.b64encode(_buf.getvalue()).decode()
            except Exception:
                pass
        else:
            doubao_chat = {'doubao-v1': 'ep-20260621182142-6x4lh'}
            model_id = doubao_chat.get(model_name, model_name)
            models = [m for m in [model_id, 'glm-4v-flash'] if m]
    elif active == 'qwen':
        if not endpoint:
            endpoint = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        models = [m for m in [model_name, 'glm-4v-flash'] if m] if model_name else ['qwen3.5-omni-flash', 'glm-4v-flash']
    else:  # glm
        if not endpoint:
            endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        models = [m for m in [model_name, 'glm-4v-flash'] if m] if model_name else ['glm-4v-flash']

    # 统一提示词 — 所有模型用同一套详细版
    prompt = """你现在处理的是软件截图内的竖向表格，执行流程：
1. 第一步：版面分析，识别表格表头、独立商品行、单元格边界；自动过滤侧边菜单、按钮文字、时间、价格、"查看""统计中""更新记录"等所有无关干扰文字。
2. 第二步：按规则匹配字段，每行商品生成一条对象，输出标准JSON数组：
字段规则：
- name：商品名称，无则填null
- stock：仓库总库存，只取表头为「仓库总库存」的列，只提取纯数字，文本带"份"自动剔除单位，无数值填0
- sales：仓库预估总销售数，只取表头为「仓库预估总销售数」的列，只提取纯数字，文本带"份"自动剔除单位，无数值填0
- region：省份名称（山东/云南这类），表格无省份列统一填null
表头冲突规则：
1. 同时存在「仓库总库存」「仓库销售库存」，只读取「仓库总库存」列；
2. 同时存在「仓库预估总销售数」「仓库总销售数」，只读取「仓库预估总销售数」列；
异常兜底：
1. 单元格文字为"统计中"等非数字内容，stock/sales统一填0；
2. 当前图片无目标订货表格、无有效商品数据，仅输出[]，禁止额外文字解释；
3. 仓库总库存为0是绝对真实的业务数据，如实提取0即可，严禁用任何其他列的数值替代；
4. **最后警告**：严禁用仓库销售库存数据替代仓库总库存数据！仓库总库存列显示多少就是多少，是0就填0！擅自替代别人的数据来充数会导致识别结果完全作废！
格式强制要求：
1. 仅输出纯净JSON，不要任何前置/后置说明、注释、换行描述；
2. stock、sales字段必须为数字类型，不能是字符串；
3. 严格按行匹配：同一行商品的库存、预估销量必须绑定本行，禁止跨行列错位匹配；"""
    max_tok = 1024

    for attempt, mdl in enumerate(models):
        # 如果fallback是智谱模型但当前走的是阿里/豆包端点，切换endpoint + 格式
        cur_endpoint = endpoint
        cur_key = key
        cur_responses = use_responses
        if 'glm' in mdl and 'dashscope' in cur_endpoint:
            cur_endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
            cur_key = providers.get('glm', {}).get('api_key', '') if isinstance(providers, dict) else ''
            if not cur_key:
                continue
            cur_responses = False
        elif 'glm' in mdl and 'ark' in cur_endpoint:
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
                resp = requests.post(cur_endpoint,
                        headers={'Authorization': f'Bearer {cur_key}', 'Content-Type': 'application/json'},
                    json={
                        'model': mdl,
                        'messages': [{'role': 'user', 'content': [
                            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
                            {'type': 'text', 'text': prompt}
                        ]}],
                        'temperature': 0.0, 'max_tokens': max_tok,
                        'thinking': {'type': 'disabled'}
                    }, timeout=60)
                data = resp.json()
                if 'choices' not in data:
                    if attempt == 0:
                        continue
                    raise RuntimeError(f"OCR失败: {data}")
                content = data['choices'][0]['message']['content']
            clean = _clean_json(content)
            items = json.loads(clean)
            validated = _validate_items(items)
            # qwen3.5-omni-flash 返回字段名兼容
            if not validated and isinstance(items, list):
                for it in items:
                    if 'goods_name' in it:
                        it['name'] = it.get('goods_name', it.get('name', ''))
                    if 'sales_volume' in it:
                        it['sales'] = it.get('sales_volume', it.get('sales', 0))
                    if 'area' in it:
                        it['region'] = it.get('area', it.get('region', ''))
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


def ocr_screenshot_crosscheck(image_path: str, model: str = 'glm-4v-flash') -> list:
    """
    交叉验证：同一张图识别两次，只采信两轮一致的商品。
    Doubao-Seed-2.1-pro 已禁用深度思考，准确度够，跳过交叉验证只调一次。
    """
    # 如果使用 Responses API，单次识别即返回（thinking:disabled 已足够准确）
    api_cfg = get_api_config()
    active = api_cfg.get('active_provider', '')
    providers = api_cfg.get('providers', {})
    provider = (providers.get(active, {}) or {}) if isinstance(providers, dict) else {}
    endpoint = provider.get('endpoint', '')
    # Doubao Responses API 和 GLM（thinking:disabled）单次识别即返回
    if (active == 'doubao' and 'responses' in endpoint) or active == 'glm':
        items = ocr_screenshot(image_path, model)
        if not items:
            items = ocr_screenshot(image_path, model)
        return items
    
    items1 = ocr_screenshot(image_path, model)
    if not items1:
        # 第一轮为空，可能是图片有问题，再试一次
        items1 = ocr_screenshot(image_path, model)
        if not items1:
            return []
    
    try:
        items2 = ocr_screenshot(image_path, model)
    except Exception:
        return items1
    
    if not items2:
        return items1
    
    def fingerprint(item):
        name = str(item.get('name', '')).strip()
        return f"{name}|{item.get('stock',0)}|{item.get('sales',0)}"
    
    fp1 = {fingerprint(it): it for it in items1}
    fp2 = {fingerprint(it): it for it in items2}
    
    common = set(fp1.keys()) & set(fp2.keys())
    if len(common) >= max(len(fp1), len(fp2)) * 0.5:
        return [fp1[k] for k in common]
    
    # 交集太少，可能是模型状态异常，再试一轮（等待更久）
    try:
        time.sleep(2)  # 冷却
        items3 = ocr_screenshot(image_path, model)
        if not items3:
            time.sleep(2)
            items3 = ocr_screenshot(image_path, model)
        if items3:
            fp3 = {fingerprint(it): it for it in items3}
            common3 = (set(fp1.keys()) & set(fp3.keys())) | (set(fp2.keys()) & set(fp3.keys()))
            if common3:
                result = {}
                for k in common3:
                    result[k] = fp1.get(k) or fp2.get(k) or fp3.get(k)
                return list(result.values())
    except (RuntimeError, json.JSONDecodeError, requests.RequestException):
        pass
    
    return items1 if len(items1) >= len(items2) else items2


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
    path = export_results(plans, schedule, os.path.join(get_base_dir(), 'output'))
    print(f'\n导出: {path}')
