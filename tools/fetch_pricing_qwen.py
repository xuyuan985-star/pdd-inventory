# -*- coding: utf-8 -*-
"""PDD EZ — 通义千问官方价目抓取器（v1.5.12 计价系统）

从 https://www.qianwenai.com/models 抓取 Qwen 模型 API 价目（每百万 tokens，元），
输出 {模型slug: {input_per_million, output_per_million}} 结构——与
settings usage.pricing 的 provider 内层结构一致，可直接合并使用。

用法：
    python tools/fetch_pricing_qwen.py [--out output/pricing_qwen.json]

说明：
- 价格源：官方模型详情页 HTML（阿里云 lowcode 渲染，含完整价格表）
- 取「输入 / 输出」主档（<=32k 默认档）；缓存命中/Batch 等旁路价格不取（_pricing.py
  口径为标准 API 调用）
- 网络失败/解析失败按模型跳过并在 stderr 提示（不中断整批）
- 价格会随官方调整——本脚本可随时重跑刷新

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
EXTRA_SLUGS = [
    'qwen3-vl-plus', 'qwen3-vl-plus-latest', 'qwen3-vl-max',
    'qwen3.5-ocr', 'qwen-vl-ocr', 'qwen-vl-ocr-latest',
    'qwen3-omni-flash', 'qwen3.5-omni-flash', 'qwen2.5-vl-32b-instruct',
    'qwen3-max', 'qwen3-flash', 'qwen3.8-flash',
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
    """
    out = {}
    pat = re.compile(
        r'priceName">([^<]+)</span>.*?priceMenuItemPriceSymbol">¥</span>'
        r'<span>([0-9.]+)</span>', re.S)
    for m in pat.finditer(html):
        name = m.group(1).strip()
        if name == '输入' and 'input_per_million' not in out:
            out['input_per_million'] = float(m.group(2))
        elif name == '输出' and 'output_per_million' not in out:
            out['output_per_million'] = float(m.group(2))
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