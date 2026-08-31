"""PDD EZ v1.6.0 — Prompt 文件化（TC-Q5）。

依据 docs/PLAN_v160.md §1.5 WS-Q5：把 ocr.py / vision.py 中 12 处内联 prompt
抽到 prompts/ 目录 4 文件 + manifest 统一管理。

宪法引用：
- §1 全列识别：prompt 内容绝不可含 columns 列清单字面量（仅当调用方按"指定列"
  模式传 {cols} 占位时，调用方负责参数化；模板只声明"严格按这些列名作为 JSON key"）。
- §4 失败哲学：load_prompt 缺失抛 KeyError，绝不静默回退内联旧串。
- §6 版本规则：prompt_version() 返回 'v160'，与 utils.VERSION 同号。

公共 API：
- load_prompt(key, variant='full') -> str：读 prompt 原文（带 mtime 缓存；缺失抛 KeyError）。
  多变体文件需指定 variant（详见 manifest.list_variants）。
- prompt_version() -> str：返回 'v160'（贯通 §1.4 Run ID 与 §1.1 Golden 评估）
- list_prompts() -> list[str]：返回 manifest 中的全部 key（供测试与诊断）
- list_variants(key) -> list[str]：返回某 prompt 的全部变体名（测试用）
"""
from .manifest import (
    load_prompt,
    prompt_version,
    list_prompts,
    list_variants,
    _cache_info,
)

__all__ = ['load_prompt', 'prompt_version', 'list_prompts', 'list_variants']