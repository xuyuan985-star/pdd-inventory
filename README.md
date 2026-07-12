# PDD EZ — 拼多多补货排期助手 v2.1

拼多多商家后台的桌面端库存管理工具。截图 OCR 识别订货管理页面，自动计算补货时间和数量，导出 Excel。

## 功能

| 功能 | 说明 |
|------|------|
| **截图识别** | 一键截取 PDD 商家后台，AI 自动提取商品/库存/销量 |
| **多种 OCR 模型** | 千问 3.5 / 豆包 v1 / 豆包 Seed 2.1 Pro / GLM-4V 可选 |
| **补货排期** | 自动计算补货时间和建议数量（红→立刻、黄→近期、绿→安全） |
| **多地区管理** | 按发货地区分类，支持批量识别和商品运输时效设置 |
| **Excel 导出** | 结果追加到桌面 PDD补货记录.xlsx，按日期分 Sheet |
| **窗口自适应** | 左侧导航栏路由，设置页免弹窗 |

## 使用方法

1. 双击 `PDD EZ v2.1.exe`
2. 打开拼多多商家后台 → 订货管理页面
3. 点 **实时截图**，窗口最小化后自动截屏识别
4. 确认数据无误 → 点 **刷新计算**
5. 导出 Excel

## 公式

```
补货时间 = 库存 ÷ 当天销量 - (运输天数 + 1)
补货量 = 日销量 × 8
```

| 补货时间 | 颜色 | 行动 |
|---------|------|------|
| ≤ 0 | 🔴 红 | 立刻补货 |
| 1 - 2 | 🟡 黄 | 近期补货 |
| > 2 | 🟢 绿 | 暂不补 |

## 技术栈

Python · tkinter · PyInstaller · PyAutoGUI · OpenAI/智谱/阿里百炼 API

## 打包

```bash
pip install pyinstaller openpyxl pillow requests numpy opencv-python pyautogui pyperclip pygetwindow
pyinstaller PDD补货助手.spec
```

输出在 `dist/PDD EZ v2.1.exe`（~75MB）
