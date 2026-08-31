# -*- coding: utf-8 -*-
"""PDD EZ — 通义千问官方价目抓取器（v1.5.12 计价系统 · v1.6.0+t4 增补）

从 https://www.qianwenai.com/models 抓取 Qwen 模型 API 价目（每百万 tokens，元），
输出 {模型slug: {input_per_million, output_per_million}} 结构——与
settings usage.pricing 的 provider 内层结构一致，可直接合并使用。

用法：
    python tools/fetch_pricing_qwen.py [--out output/pricing_qwen.json]

说明：
- **价格源**：官方模型广场 https://www.qianwenai.com/models 详情页 HTML（列表页 +
  单模型详情页 /models/<slug>，CSS 类名前缀哈希化但锚点 `priceName">输入</span>` 与
  `priceMenuItemPriceSymbol">¥</span><span>` 不受影响 —— 历史 v1.5.12 抓取器 +
  v1.6.0+t4 omni-flash Fallback 兼容）；aliyun 帮助中心
  https://help.aliyun.com/zh/model-studio/<slug> 仅作**核验源**（与 qianwenai.com
  报价对照，本工具不抓取 —— aliyun 帮助中心为低代码渲染，部分页面需登录才能拿到
  完整价目）。
- **档位说明**：本工具取「输入 / 输出」**主档**（默认档，单一价格）—— 同一模型可能
  在 aliyun 帮助中心列出多档（闲时/忙时/缓存命中/Batch/快照版本），用户可在设置页
  按需细分覆盖；本工具不抓这些细分档。
  - 例：`deepseek-v4-flash-0731` 在 qianwenai.com 列表页为唯一 deepseek slug，
    抓取值 1.5/4.5（与 aliyun 帮助中心「华北2北京·0731 快照闲时档」一致）；
    `deepseek-v4-flash`（无日期后缀，默认档）在 aliyun 帮助中心为 1.0/2.0。
- **兼容性**：HTML 结构变化时 regex 不命中 → 单模型 skip + stderr 提示（不中断整批）。
- **重运行**：价格会随官方调整 —— 本脚本可随时重跑刷新。
- **失败安全**：网络失败/解析失败按模型跳过，不冒充旧数据。

输出文件不入仓（output/ 已在 .gitignore）。
"""
import argparse
import json
import os
import re
import sys

import requests

LIST_URL = 'https://www.qianwenai.com/models'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
_TIMEOUT = 25

# 列表页只展示模型市场子集；补充 API 常用模型 slug（详情页模式 /models/<slug>，
# 不存在的页面返回 404 自动跳过，不影响整批）。按需增补。
# v1.6.0+t4（发布价目刷新）：按实际抓取结果整理，剔除官方已下架 slug；
# 增补 qwen3.7-max / qwen-vl-max / qwen-vl-plus / qvq-max 等实际可访问的视觉/OCR 系。
EXTRA_SLUGS = [
    # 视觉（vl/ocr）
    'qwen3-vl-plus', 'qwen-vl-plus', 'qwen-vl-max',
    'qwen3.5-ocr', 'qwen-vl-ocr', 'qwen-vl-ocr-latest',
    # 全模态（omni）
    'qwen3-omni-flash', 'qwen3.5-omni-flash',
    # 文本主力
    'qwen3-max', 'qwen3.7-max',
    # 推理（qvq）
    'qvq-max',
]


def fetch_model_slugs() -> list:
    """列表页 → 全部模型 slug（按出现顺序去重）+ EXTRA_SLUGS 补全。"""
    r = requests.get(LIST_URL, headers=UA, timeout=_TIMEOUT)
    r.raise_for_status()
    slugs = []
    for m in re.finditer(r'href="https://www\.qianwenai\.com/models/([a-z0-9][a-z0-9.\-]*)["?]', r.text):
        if m.group(1) not in slugs:
            slugs.append(m.group(1))
    for s in EXTRA_SLUGS:
        if s not in slugs:
            slugs.append(s)
    return slugs


def parse_detail_pricing(html: str) -> dict:
    """详情页 HTML → {input_per_million, output_per_million}（主档，缺项则缺键）。

    结构：<ul class="...priceMenu"><li class="...priceMenuItem">
      <span class="...priceName">输入|输出|输入（缓存命中）|...</span>
      <...>¥<span>1</span></span>.../M tokens</div></li>
    取 priceName 恰为「输入」「输出」的两项（默认档，忽略缓存/Batch/限时）。

    v1.6.0+t4 增补：omni-flash 系列使用细分价格名（"输入：文本"、"输出：文本" 等），
    取**第一个**"输入：..." 与 **第一个**"输出：..."。无细分名时 fallback 通用 "输入/输出"。
    """
    out = {}
    pat = re.compile(
        r'priceName">([^<]+)</span>.*?priceMenuItemPriceSymbol">¥</span>'
        r'<span>([0-9.]+)</span>', re.S)
    # 1) 优先精确匹配 「输入」「输出」（大多数 vl/ocr/主力模型）
    for m in pat.finditer(html):
        name = m.group(1).strip()
        if name == '输入' and 'input_per_million' not in out:
            out['input_per_million'] = float(m.group(2))
        elif name == '输出' and 'output_per_million' not in out:
            out['output_per_million'] = float(m.group(2))
    # 2) Fallback：omni-flash 等细分价格 → 取第一个「输入：...」与「输出：...」
    if 'input_per_million' not in out or 'output_per_million' not in out:
        for m in pat.finditer(html):
            name = m.group(1).strip()
            if name.startswith('输入') and 'input_per_million' not in out:
                out['input_per_million'] = float(m.group(2))
            elif name.startswith('输出') and 'output_per_million' not in out:
                out['output_per_million'] = float(m.group(2))
            if 'input_per_million' in out and 'output_per_million' in out:
                break
    return out


def fetch_all(slugs, limit=None) -> dict:
    """逐模型抓取。失败模型入 errors。"""
    pricing = {}
    errors = []
    for i, slug in enumerate(slugs):
        if limit and i >= limit:
            break
        try:
            r = requests.get(f'{LIST_URL}/{slug}', headers=UA, timeout=_TIMEOUT)
            if r.status_code != 200:
                errors.append((slug, f'HTTP {r.status_code}'))
                continue
            p = parse_detail_pricing(r.text)
            if p:
                pricing[slug] = p
            else:
                errors.append((slug, '解析不到价格表'))
        except Exception as e:
            errors.append((slug, f'{type(e).__name__}: {str(e)[:80]}'))
        sys.stderr.write(f'[{i + 1}/{len(slugs)}] {slug} {"OK" if slug in pricing else "skip"}\n')
    return pricing, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='output/pricing_qwen.json')
    ap.add_argument('--limit', type=int, default=0, help='只抓前 N 个模型（调试用）')
    args = ap.parse_args()

    print('抓取模型列表...')
    slugs = fetch_model_slugs()
    print(f'共 {len(slugs)} 个模型: {", ".join(slugs[:12])}{"..." if len(slugs) > 12 else ""}')

    pricing, errors = fetch_all(slugs, limit=args.limit or None)
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(pricing, f, ensure_ascii=False, indent=2)
    print(f'成功 {len(pricing)} 个模型 → {args.out}')
    if errors:
        print(f'失败 {len(errors)} 个:')
        for slug, why in errors[:10]:
            print(f'  - {slug}: {why}')


if __name__ == '__main__':
    main()