"""
PDD 后台截图 OCR 识别
输入：PDD订货管理页面截图
输出：[{name, stock, sales}, ...]
"""

import base64, json, os, sys, time

# 密钥加载：优先本地 api_keys.py（gitignored），缺失时退回环境变量
try:
    from api_keys import _get_key
except ImportError:
    def _get_key(service):
        env_map = {'zhipu': 'ZHIPU_API_KEY', 'ark': 'ARK_API_KEY', 'qwen': 'DASHSCOPE_API_KEY'}
        return os.environ.get(env_map.get(service, ''), '')


def _get_api_key() -> str:
    """优先自定义API → 环境变量 → 内置key"""
    import os, sys, json
    # 0. 从 settings.json 读取自定义API
    try:
        if getattr(sys, 'frozen', False):
            base = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'PDD补货助手')
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        sf = os.path.join(base, 'settings.json')
        if os.path.exists(sf):
            with open(sf, 'r', encoding='utf-8') as f:
                s = json.load(f)
            api_cfg = s.get('api', {})
            if api_cfg.get('mode') == 'custom' and api_cfg.get('key'):
                return api_cfg['key']
    except Exception: pass
    # 1. 环境变量
    key = os.environ.get('ZHIPU_API_KEY', '')
    if key: return key
    # 2. 内置默认key（由 api_keys 模块管理）
    return _get_key('zhipu')


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
        name = str(item.get('name', '')).strip()
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
        region = str(item.get('region', '')).strip()
        # 校验：库存>10000且库存/日销>50 → 疑似取错列（仓库销售库存），丢弃
        if sales > 0 and stock > 10000 and stock / sales > 50:
            continue  # 丢弃可疑数据
        if stock > 0 or sales > 0:
            cleaned.append({'name': name, 'stock': stock, 'sales': sales, 'region': region})
    
    if not cleaned:
        return []
    
    # ── 幻觉数据过滤器 ──
    KNOWN_REGIONS = {'云南','广东','浙江','北京','上海','江苏','山东','四川','湖北','湖南','河南','河北',
                     '福建','安徽','辽宁','陕西','重庆','江西','广西','贵州','山西','吉林','黑龙江',
                     '甘肃','内蒙古','新疆','海南','宁夏','青海','西藏','天津','香港','澳门','台湾'}
    
    # 检查1：所有地区都是假地名 → 幻觉（宽容省/市后缀）
    regions_found = {it['region'].rstrip('省市自治区') for it in cleaned if it['region']}
    if regions_found and not regions_found & KNOWN_REGIONS:
        return []
    
    # 检查2：所有商品的stock和sales都一模一样 → 幻觉
    stocks = {it['stock'] for it in cleaned}
    sales_set = {it['sales'] for it in cleaned}
    if len(stocks) == 1 and len(sales_set) == 1 and len(cleaned) >= 2:
            return []  # 多个商品库存/销量完全相同，极可疑
    
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


def _get_api_config():
    """读取API配置（模式+key）"""
    import os, sys, json
    try:
        if getattr(sys, 'frozen', False):
            base = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'PDD补货助手')
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        sf = os.path.join(base, 'settings.json')
        if os.path.exists(sf):
            with open(sf, 'r', encoding='utf-8') as f:
                return json.load(f).get('api', {})
    except Exception: pass
    return {}


def ocr_screenshot(image_path: str, model: str = 'glm-4v-flash') -> list:
    """
    识别 PDD 后台截图。自动根据设置选择 API：
    - 默认/自定义: 智谱 GLM-4V
    - qwen: 阿里百炼 qwen3.5-ocr
    """
    with open(image_path, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()

    api_cfg = _get_api_config()
    mode = api_cfg.get('mode', 'builtin')
    builtin_model = api_cfg.get('builtin_model', '')
    use_responses = False  # 默认 chat/completions
    
    if mode == 'custom':
        builtin_model = api_cfg.get('custom_model', '')  # custom 模式取 custom_model 映射 prompt
        key = api_cfg.get('key', '')
        # 根据模型名判断endpoint
        cm = api_cfg.get('custom_model', '')
        if 'doubao' in cm.lower() or 'ark' in cm.lower():
            endpoint = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
        elif 'glm' in cm:
            endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        else:
            endpoint = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        models = [cm, 'glm-4v-flash']
    else:  # builtin
        builtin_model = api_cfg.get('builtin_model', 'qwen3.5-ocr')
        use_responses = False  # 默认 chat/completions，仅 Doubao-Seed-2.1-pro 走 Responses
        if builtin_model.lower().startswith('doubao'):
            key = 'ark-8c5d35c3-a117-4b2b-b93a-a43dbe0f7df5-f55c7'
            # Doubao-Seed-2.1-pro 用 Responses API（thinking:disabled 提速），v1 用 chat/completions
            if builtin_model == 'Doubao-Seed-2.1-pro':
                use_responses = True
                endpoint = 'https://ark.cn-beijing.volces.com/api/v3/responses'
                models = ['ep-20260710230432-jccpg', 'glm-4v-flash']
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
                    pass  # fallback 用原图
            else:
                use_responses = False
                endpoint = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
                # chat/completions 系列模型映射
                doubao_chat = {
                    'doubao-v1': 'ep-20260621182142-6x4lh',
                }
                model_id = doubao_chat.get(builtin_model, 'ep-20260621182142-6x4lh')
                models = [model_id, 'glm-4v-flash']
        elif builtin_model.startswith('qwen'):
            key = 'sk-ws-H.RPREILI.vcEX.MEQCIHemJ7bO8iUT5_2HHJOYiahN-KKzZIkPNCXZtHOkO4tgAiB0KZnPJtu0bhokUkD4TTMEppZZJdUZl6ltJKpUkPJYRw'
            endpoint = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
            models = [builtin_model.replace('qwen-vl-ocr', 'qwen3.5-ocr'), 'glm-4v-flash']
        else:  # glm models
            key = _get_api_key()
            endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
            models = [builtin_model, 'glm-4v-flash']
    
    if not key:
        raise RuntimeError("API Key 未设置")

    import requests
    
    # 是否付费模型（精简提示词省token）
    is_paid = any('qwen' in m or 'doubao' in m or 'glm-4v-plus' in m or m.startswith('ep-') for m in models)
    
    if is_paid:
        # 付费模型提示词：Doubao 系列用增强版，qwen 用原版
        if builtin_model.lower().startswith('doubao'):
            prompt = """PDD订货管理表格OCR。重要：表格可能有多列库存数据，严格按以下规则提取：
1. name: 商品名称（第一列）
2. stock: 只取「仓库总库存」列！跳过「仓库销售库存」！认准"总"字！
3. sales: 只取「仓库预估总销售数」列！跳过「仓库总销售数」！认准"预估"二字！
4. region: 省份名（如山东、云南）
输出JSON数组，取错列会导致数据作废。非表格数据返[]。"""
        else:
            prompt = "提取表格每行：商品名、仓库总库存(注意总字!不是仓库销售库存)、仓库预估总销售数(含预估二字!不是仓库总销售数)、省份名。JSON:[{name,stock,sales,region}]。无关返[]"
        max_tok = 200
    else:
        prompt = """你是拼多多仓库数据提取器。截图是商家后台「订货管理」页面。

首先判断：截图中是否包含拼多多商家后台的「订货管理」数据表格？
如果截图是无关页面（如桌面、浏览器空白页、其他网站），返回空数组 []。

在页面中央有一个数据表格，每一行是一个商品。表格有多列数值，请仔细区分：

1. 商品名称 — 表格最左侧，完整名称
2. 【仓库总库存】— 表头精确包含"仓库总库存"，注意认准"总"字！
   ⚠️ PDD后台可能有"仓库总库存"和"仓库销售库存"两列，请严格取"仓库总库存"列
3. 【仓库预估总销售数】— 表头含"仓库预估"！不是"仓库总销售数"！数字表示当天预估销量
4. 发货地区 — 表头含"销售地区"或"地区"或"省份"，取省份名

严格要求：数字精确匹配、跳过表头汇总行、无关页面返[]

返回纯JSON：[{"name":"商品名","stock":整数,"sales":整数,"region":"省份"}]"""
        max_tok = 500

    for attempt, mdl in enumerate(models):
        # 如果fallback是智谱模型但当前走的是阿里/豆包端点，切换endpoint + 格式
        cur_endpoint = endpoint
        cur_key = key
        cur_responses = use_responses
        if 'glm' in mdl and 'dashscope' in cur_endpoint:
            cur_endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
            cur_key = _get_api_key()
            cur_responses = False
        elif 'glm' in mdl and 'ark' in cur_endpoint:
            cur_endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
            cur_key = _get_api_key()
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
                        'temperature': 0.0, 'max_tokens': max_tok
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
            # qwen-vl-ocr / qwen3.5-ocr 返回字段名兼容
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
    # Doubao-Seed-2.1-pro 单次识别即返回（thinking:disabled 已足够准确）
    api_cfg = _get_api_config()
    if api_cfg.get('builtin_model') == 'Doubao-Seed-2.1-pro':
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
        name = str(item.get('name', '')).strip()[:20]
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
    except Exception: pass
    
    return items1 if len(items1) >= len(items2) else items2


def locate_element(image_path: str, description: str) -> dict:
    """
    用 GLM-4V 定位截图中 UI 元素的位置，返回屏幕坐标。
    description: 要寻找的元素描述，如 "蓝底白字的查询按钮" 或 "销售地区下拉框"
    返回: {'x': 中心x, 'y': 中心y, 'width': 宽, 'height': 高} 或 None
    """
    with open(image_path, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()
    
    key = _get_api_key()
    if not key:
        raise RuntimeError("ZHIPU_API_KEY 未设置")
    
    import requests
    
    prompt = f"""请在截图中找到"{description}"这个UI元素的位置。
返回该元素的像素坐标，格式为JSON：
{{"found": true, "x": 元素中心x坐标, "y": 元素中心y坐标, "width": 宽度, "height": 高度}}

如果找不到该元素，返回：
{{"found": false}}

注意：坐标是相对于截图的像素坐标，截图尺寸需自行判断。"""
    
    try:
        resp = requests.post('https://open.bigmodel.cn/api/paas/v4/chat/completions',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={
                'model': 'glm-4v',
                'messages': [{'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
                    {'type': 'text', 'text': prompt}
                ]}],
                'temperature': 0.0
            }, timeout=30)
        
        data = resp.json()
        if 'choices' not in data:
            return None
        
        content = data['choices'][0]['message']['content']
        result = json.loads(_clean_json(content))
        if result.get('found'):
            return {
                'x': int(result['x']),
                'y': int(result['y']),
                'width': int(result.get('width', 30)),
                'height': int(result.get('height', 20)),
            }
        return None
    except Exception:
        return None


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

    # Use _base_dir for output path consistency
    def _base_dir():
        if getattr(sys, 'frozen', False):
            data_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'PDD补货助手')
            os.makedirs(data_dir, exist_ok=True)
            return data_dir
        return os.path.dirname(os.path.abspath(__file__))

    inventory = [{'sku': i['name'][:12], 'name': i['name'], 'stock': i['stock']} for i in items]
    sales = {i['name'][:12]: {'sales': i['sales']} for i in items}

    plans = calculate_replenishment(inventory, sales)
    schedule = generate_schedule(plans)
    path = export_results(plans, schedule, os.path.join(_base_dir(), 'output'))
    print(f'\n导出: {path}')
