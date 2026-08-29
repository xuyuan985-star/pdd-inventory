# PDD EZ — 拼多多补货排期助手

拼多多商家后台的桌面端库存管理工具：截图 OCR 识别订货管理页面，自动计算补货时间和数量，导出 Excel。也支持直接导入 CSV/XLSX 表格计算。

## 快速上手

1. 双击 `PDD EZ.exe` 启动
2. **实时截图** 或 **📥 导入表格**（二选一入口）
3. 确认数据 → **刷新计算** → **导出 Excel**

## 核心功能

- **截图识别**：一键截取 PDD 商家后台，AI 自动提取商品/库存/销量，自动计算补货时间与数量
- **表格导入**：CSV / XLSX 结构化导入，列映射预览可改，非 PDD 表格也能用
- **历史趋势**：识别/导入数据本机保存（SQLite，不上传），按日/地区汇总，单商品库存趋势折线
- **用量明细**：API 消耗实时显示（本次/本月），按模型/用途聚合估算费用
- **双模型验证**：主+副模型交叉比对，不一致标 ⚠
- **官方总数权威停止**：读右下角"共有N条"，识别齐全自动结束滚动
- **批量识别**：多省份独立识别，F9 紧急停止
- **补货策略**：经典 / 加权（近 7/14/30 日销量加权）两种模型

## 模型配置

- 设置 → API 管理 配置主模型。推荐通用视觉模型：`qwen3-vl-plus` / `qwen3-vl-max`
- OCR 专用模型（`qwen*-ocr`）只做文字提取，不能做定位/结构化 JSON——配为主模型时定位自动切副模型

## 技术栈

Python · tkinter · PyInstaller · PyAutoGUI · OpenCV · 多厂商视觉 API

## 打包

```bash
pyinstaller "PDD补货助手.spec"   # 主程序 onedir
pyinstaller updater.spec        # 更新器
python _build_update_zip.py     # 增量包（git diff 驱动）
```

发布走 GitHub Release（`gh release create vX.Y` 传资产）。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)

## 设计标准

改代码前必读 [docs/DESIGN.md](docs/DESIGN.md)