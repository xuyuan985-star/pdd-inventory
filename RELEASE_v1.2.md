# PDD EZ v1.2 — Release 发布材料

> ⚠️ **历史版本发布材料**（当前版本为 v1.4，见 README/CHANGELOG）。
> 保留此文件仅作 v1.2 历史记录；v1.4 发布材料按当前 dist/ 产物生成。

## 1. GitHub Release 基本信息

- **Tag**: `v1.2`
- **目标分支**: `main`（已推送）
- **标题**: `PDD EZ v1.2 — OCR 精度大升级 + 稳定性修复`
- **前置产物**（需先打包，见第 3 节）:
  - `dist/PDD EZ v1.2/`（PyInstaller onedir）
  - `dist/PDD EZ Updater.exe`（更新器）
  - `dist/PDD_EZ_v1.2.zip`（全量安装包）
  - `dist/PDD_EZ_v1.2.zip.sha256`
  - `dist/PDD_EZ_v1.2_update.zip`（增量包，如适用）
  - `dist/PDD_EZ_v1.2_update.zip.sha256`

## 2. Release 正文（可直接粘贴）

### 🔍 OCR 识别精度大幅提升
- 自适应表格检测：自动定位表格区域，不再依赖固定裁剪
- 提示词重构 + 示例引导：模型"抄写"原文，数字解析交给代码
- 数字解析支持千分位、单位（万/千/w/k）、全角、约/共 前缀
- 列对齐校验：检测并修正 stock/sales 错位
- 双模型交叉验证（可选开关）：主+副模型比对，低置信度商品 ⚠ 标红
- 幻觉过滤器优化：不再误杀 2 字商品名、纯英文 SKU、同系列商品

### 🖥️ 多分辨率适配
- 模板匹配 7 档尺度（0.5x~1.5x），支持 4K/带鱼屏
- 点击偏移按分辨率自动缩放

### 🛡️ 稳定性与安全
- 修复导出 Excel 崩溃、批量识别线程崩溃、更新器自升级失效等 60+ 问题
- Excel 公式注入防护、更新包安全校验（SHA256/路径防护/解压上限）
- 补货偏移量可配置（settings 中 `replenishment_offset`）
- 批量识别失败不再静默，全失败有明确提示

### 🎨 体验优化
- 主题切换即时生效（已访问页面同步刷新）
- 分辨率预设恢复 UI 入口（通用设置页）
- 批量识别操作自动重试、临时文件自动清理
- 港澳台地区批量识别支持

## 3. 打包命令（发布前执行）

```bash
# 1) 主程序 onedir（输出 dist/PDD EZ v1.2/）
pyinstaller "PDD补货助手.spec" --noconfirm

# 2) 更新器（输出 dist/PDD EZ Updater.exe）
pyinstaller updater.spec --noconfirm

# 3) 全量 zip + sha256
#    打包 dist/PDD EZ v1.2 → PDD_EZ_v1.2.zip，并生成 .sha256

# 4) 增量包（依赖不变时约 7MB；v1.0→v1.1 结构变更时自动回退全量）
python _build_update_zip.py
```

## 4. 上传到 GitHub Release 的附件（Assets）

| 文件 | 大小参考 | 说明 |
|------|---------|------|
| `PDD_EZ_v1.2.zip` | ~75MB | 全量安装包（必传） |
| `PDD_EZ_v1.2.zip.sha256` | 83B | 全量包校验（必传） |
| `PDD EZ Updater.exe` | ~65MB | 更新器（必传） |
| `PDD_EZ_v1.2_update.zip` | ~7MB | 增量包（如适用） |
| `PDD_EZ_v1.2_update.zip.sha256` | ~90B | 增量包校验（如适用） |

## 5. 注意事项

- 增量包仅当 **v1.1 与 v1.2 目录结构兼容** 时上传；结构变更时更新器会自动回退全量，增量包可省略。
- 更新器自升级（zip 内嵌 PDD EZ Updater.exe）已修复，正常随主程序目录打包。
- 发布后更新器会通过 GitHub API 拉取 `latest` release，自动识别 `v1.2 > v1.1`。
