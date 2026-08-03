# PDD EZ — 拼多多补货排期助手 v1.2

拼多多商家后台的桌面端库存管理工具。截图 OCR 识别订货管理页面，自动计算补货时间和数量，导出 Excel。

## 功能

| 功能 | 说明 |
|------|------|
| **截图识别** | 一键截取 PDD 商家后台，AI 自动提取商品/库存/销量 |
| **自适应表格检测** | OpenCV 自动定位表格区域，不再依赖固定裁剪，识别更纯净 |
| **多种 OCR 模型** | 千问 / 豆包 / GLM-4V 可选，模型失败自动切换备用模型 |
| **双模型验证** | 主模型 + GLM 双路交叉比对，结果不一致标 ⚠ 提示并取保守值 |
| **补货排期** | 自动计算补货时间和建议数量（红→立刻、黄→近期、绿→安全） |
| **多地区管理** | 按发货地区分类，支持批量识别（含港澳台）和运输时效设置 |
| **AI 智能定位** | 自动识别页面元素坐标，4K/带鱼屏自适应，多次采样自适应 |
| **Excel 导出** | 结果追加到 PDD补货记录.xlsx，按日期分 Sheet（防重名） |

## 使用方法

1. 双击 `PDD EZ v1.2.exe`
2. 打开拼多多商家后台 → 订货管理页面
3. 点 **实时截图**，窗口最小化后自动截屏识别
4. 确认数据无误 → 点 **刷新计算**
5. 导出 Excel

## 批量识别

- **设置 → 校准**：AI 智能定位（自动识别按钮坐标）（唯一模式，自动识别自适应分辨率）
- 批量识别勾选 **🛡 双模型验证**：识别更准，但耗时约翻倍
- 识别失败会明确提示原因，不再静默卡死

## 公式

```
补货时间 = 库存 ÷ 当天销量 - (运输天数 + replenishment_offset)
补货量 = 日销量 × 8
```

- `replenishment_offset` 默认 1，可在 `settings.json` 配置（CLI 与 GUI 路径一致）

| 补货时间 | 颜色 | 行动 |
|---------|------|------|
| ≤ 0 | 🔴 红 | 立刻补货 |
| 1 - 2 | 🟡 黄 | 近期补货 |
| > 2 | 🟢 绿 | 暂不补 |

## 技术栈

Python · tkinter · PyInstaller · PyAutoGUI · OpenCV · OpenAI/智谱/阿里百炼 API

## 打包

```bash
pip install pyinstaller openpyxl pillow numpy opencv-python pyautogui pyperclip pygetwindow
pyinstaller "PDD补货助手.spec"   # 主程序 onedir
pyinstaller updater.spec        # 更新器
python _build_update_zip.py     # 增量更新包（git diff 驱动）
```

输出在 `dist/PDD EZ v1.2/`（全量 ~79MB，增量包含新 EXE+更新器）

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)
