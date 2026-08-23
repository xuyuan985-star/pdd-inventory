"""PDD EZ 核心模块 smoke tests——后续轮次改动的验证锚点"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class TestLogger(unittest.TestCase):
    def test_logger_module_importable(self):
        import logger
        self.assertTrue(hasattr(logger, 'Logger'))
        self.assertTrue(hasattr(logger, 'log'))

    def test_logger_writes_file(self):
        import tempfile, shutil
        from logger import Logger
        tmp = tempfile.mkdtemp()
        try:
            log = Logger(log_dir=tmp)
            log.info('test line')
            import glob
            files = glob.glob(os.path.join(tmp, '*.log'))
            self.assertTrue(files, '日志文件未生成')
            with open(files[0], encoding='utf-8') as fp:
                content = fp.read()
            self.assertIn('test line', content)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGithubApi(unittest.TestCase):
    def test_mirror_url(self):
        from github_api import mirror_download_url
        u = mirror_download_url('https://github.com/x/a.zip', prefer_mirror=True)
        self.assertTrue(u.startswith('https://github.kotori.top/'))
        u2 = mirror_download_url('https://github.com/x/a.zip', prefer_mirror=False)
        self.assertTrue(u2.startswith('https://github.com/'))
        u3 = mirror_download_url('https://example.com/x.zip')
        self.assertEqual(u3, 'https://example.com/x.zip')


class TestConfigMerge(unittest.TestCase):
    def test_merge(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('pdd_utils', os.path.join(HERE, 'utils.py'))
        u = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(u)
        tpl = {'theme': '极简白', 'api': {'active_provider': 'doubao', 'providers': {}}}
        user = {'theme': '终末地', 'api': None}
        merged = u.Config._merge(tpl, user)
        self.assertEqual(merged['theme'], '终末地')
        self.assertEqual(merged['api']['active_provider'], 'doubao')

    def test_version_newer(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('pdd_utils', os.path.join(HERE, 'utils.py'))
        u = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(u)
        self.assertTrue(u.version_newer('v1.5', 'v1.4'))
        self.assertFalse(u.version_newer('v1.3', 'v1.4'))
        self.assertTrue(u.version_newer('v1.10', 'v1.9'))


class TestUpdater(unittest.TestCase):
    def test_importable(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('pdd_updater', os.path.join(HERE, 'updater.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertTrue(hasattr(m, 'do_finalize'))
        self.assertTrue(hasattr(m, '_cover_with_self_handling'))
        self.assertTrue(hasattr(m, '_pick_main_exe'))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 专属 API')
    def test_wait_pid_exit(self):
        """_wait_pid_exit：已死 PID 立即返回 True；活进程等待超时返回 False（64 位句柄安全验证）"""
        import importlib.util, subprocess, time
        spec = importlib.util.spec_from_file_location('pdd_updater', os.path.join(HERE, 'updater.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        # 不存在的 PID → 进程已死 → True
        self.assertTrue(m._wait_pid_exit(99999999, timeout=1))
        # 活进程（自己）等待 0.1s 超时 → False
        self.assertFalse(m._wait_pid_exit(os.getpid(), timeout=0.1))
        # 活进程匹配 expected_exe 路径（本进程 python）→ 等待超时 False
        self.assertFalse(m._wait_pid_exit(os.getpid(), expected_exe=sys.executable, timeout=0.1))

    def test_do_finalize_full_chain(self):
        """集成：do_finalize 全链路——解压→覆盖→配置保留→_pick_main_exe 选固定名"""
        import importlib.util, tempfile, shutil, zipfile
        spec = importlib.util.spec_from_file_location('pdd_updater', os.path.join(HERE, 'updater.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        tmp = tempfile.mkdtemp()
        try:
            appdir = os.path.join(tmp, 'PDD EZ')
            os.makedirs(os.path.join(appdir, '_internal'), exist_ok=True)
            with open(os.path.join(appdir, 'PDD EZ.exe'), 'wb') as f:
                f.write(b'old')
            with open(os.path.join(appdir, '_internal', 'settings.json'), 'wb') as f:
                f.write(b'USER_CFG')

            # 构造更新包
            stage = os.path.join(tmp, 'stage')
            os.makedirs(os.path.join(stage, 'PDD EZ', '_internal'), exist_ok=True)
            with open(os.path.join(stage, 'PDD EZ', 'PDD EZ.exe'), 'wb') as f:
                f.write(b'new')
            with open(os.path.join(stage, 'PDD EZ', '_internal', 'lib.dll'), 'wb') as f:
                f.write(b'lib')
            zip_path = os.path.join(tmp, 'upd.zip')
            with zipfile.ZipFile(zip_path, 'w') as zf:
                for root, _, files in os.walk(os.path.join(stage, 'PDD EZ')):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        zf.write(fp, os.path.relpath(fp, stage))

            rc = m.do_finalize(zip_path, os.path.join(tmp, 'ex'), appdir,
                               wait_pid=0, target_main=os.path.join(appdir, 'PDD EZ.exe'))
            self.assertEqual(rc, 0, 'finalize 应返回 0')
            with open(os.path.join(appdir, 'PDD EZ.exe'), 'rb') as f:
                self.assertEqual(f.read(), b'new', '主 exe 未更新')
            with open(os.path.join(appdir, '_internal', 'settings.json'), 'rb') as f:
                self.assertEqual(f.read(), b'USER_CFG', '用户配置被覆盖')
            picked = m._pick_main_exe(appdir)
            self.assertTrue(picked.endswith('PDD EZ.exe'), f'应选固定名: {picked}')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_finalize_with_updater_in_package(self):
        """blocker 场景：更新包含新 updater + updater 从 %TEMP% 运行（GUI 复制后）。
        验证 updater 自身可被覆盖（temp 副本不在 target_dir，rename 自身无冲突）。"""
        import importlib.util, tempfile, shutil, zipfile
        spec = importlib.util.spec_from_file_location('pdd_updater', os.path.join(HERE, 'updater.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        tmp = tempfile.mkdtemp()
        try:
            appdir = os.path.join(tmp, 'PDD EZ')
            os.makedirs(os.path.join(appdir, '_internal'), exist_ok=True)
            with open(os.path.join(appdir, 'PDD EZ.exe'), 'wb') as f:
                f.write(b'old')
            with open(os.path.join(appdir, 'PDD EZ Updater.exe'), 'wb') as f:
                f.write(b'old_updater')
            with open(os.path.join(appdir, '_internal', 'settings.json'), 'wb') as f:
                f.write(b'USER_CFG')

            # 更新包含新 updater（真实发布包含 updater）
            stage = os.path.join(tmp, 'stage')
            os.makedirs(os.path.join(stage, 'PDD EZ', '_internal'), exist_ok=True)
            with open(os.path.join(stage, 'PDD EZ', 'PDD EZ.exe'), 'wb') as f:
                f.write(b'new')
            with open(os.path.join(stage, 'PDD EZ', 'PDD EZ Updater.exe'), 'wb') as f:
                f.write(b'new_updater')
            with open(os.path.join(stage, 'PDD EZ', '_internal', 'lib.dll'), 'wb') as f:
                f.write(b'lib')
            zip_path = os.path.join(tmp, 'upd.zip')
            with zipfile.ZipFile(zip_path, 'w') as zf:
                for root, _, files in os.walk(os.path.join(stage, 'PDD EZ')):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        zf.write(fp, os.path.relpath(fp, stage))

            # 模拟 updater 从 %TEMP% 运行（sys.argv[0] 在 temp，不在 target_dir）
            old_argv0 = sys.argv[0]
            sys.argv[0] = os.path.join(tempfile.gettempdir(), 'PDD_EZ_Updater_tmp.exe')
            try:
                rc = m.do_finalize(zip_path, os.path.join(tmp, 'ex'), appdir,
                                   wait_pid=0, target_main=os.path.join(appdir, 'PDD EZ.exe'))
            finally:
                sys.argv[0] = old_argv0
            self.assertEqual(rc, 0, '含 updater 的更新应成功')
            with open(os.path.join(appdir, 'PDD EZ Updater.exe'), 'rb') as f:
                self.assertEqual(f.read(), b'new_updater', 'updater 自身未更新')
            with open(os.path.join(appdir, 'PDD EZ.exe'), 'rb') as f:
                self.assertEqual(f.read(), b'new', '主 exe 未更新')
            with open(os.path.join(appdir, '_internal', 'settings.json'), 'rb') as f:
                self.assertEqual(f.read(), b'USER_CFG', '用户配置被覆盖')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPrivacy(unittest.TestCase):
    """隐私过滤：真实测试数据不得出现在源码中"""

    SENSITIVE = ['盐渍', '鞭炮笋', '海带苗', '海带结', '烟台', '96622588033', '9662',
                 '939262347672', '984387564986', '284337564986']

    def test_no_sensitive_data(self):
        for f in sorted(os.listdir(HERE)):
            if not f.endswith(('.py', '.md', '.txt', '.json')):
                continue
            if f.startswith('_') or f == 'test_smoke.py':
                # 跳过临时扫描脚本/测试本身（其内容含敏感词列表）
                continue
            with open(os.path.join(HERE, f), encoding='utf-8', errors='replace') as fp:
                content = fp.read()
            for s in self.SENSITIVE:
                self.assertNotIn(s, content, f'{f} 含敏感数据: {s}')


class TestBugHuntRegressions(unittest.TestCase):
    """v1.4.5 bug hunt 修复回归（F4/F11 可纯函数断言的部分）"""

    def test_strip_tail_noise_keeps_pure_date(self):
        """bug hunt F4：纯日期/整值剥空时保留原文，不再清空更新时间列"""
        from ocr import strip_tail_noise
        self.assertEqual(strip_tail_noise('2024-05-04'), '2024-05-04')
        self.assertEqual(strip_tail_noise('2024年5月4日'), '2024年5月4日')
        # 常规词条剥离仍生效
        self.assertEqual(strip_tail_noise('128份 查看'), '128份')
        self.assertEqual(strip_tail_noise('示例仓库查看地址'), '示例仓库')

    def test_total_count_prefers_common_n(self):
        """bug hunt F11（fix-review C14 强化）：直接调生产 helper _parse_total_count"""
        from vision import _parse_total_count
        self.assertEqual(_parse_total_count('每页10条 共有128条'), 128)
        self.assertEqual(_parse_total_count('共有 9 条'), 9)
        self.assertEqual(_parse_total_count('总共有 5 条'), 5)
        self.assertEqual(_parse_total_count('总共25条'), 25)
        self.assertEqual(_parse_total_count('识别不了'), None)

    def test_strip_tail_noise_pure_word_regression(self):
        """fix-review N1 收窄：整值保护仅日期/数字形态，纯词条噪音仍剥空"""
        from ocr import strip_tail_noise
        self.assertEqual(strip_tail_noise('查看地址'), '')
        self.assertEqual(strip_tail_noise('更新记录'), '')
        self.assertEqual(strip_tail_noise('2024-05-04'), '2024-05-04')
        self.assertEqual(strip_tail_noise('128份 查看'), '128份')


class TestUpdaterRegression(unittest.TestCase):
    """fix-review P0/P1 回归：import re / _is_program_dir 版本号分支 / N2 0 值守卫"""

    def test_is_program_dir_version_named_no_nameerror(self):
        """P0-C1：版本号 exe + _internal 应为 True；未 import re 时此调用必然 NameError"""
        import importlib.util, os, tempfile, shutil
        spec = importlib.util.spec_from_file_location('pdd_updater2', os.path.join(HERE, 'updater.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        tmp = tempfile.mkdtemp()
        try:
            d1 = os.path.join(tmp, 'Prog1')
            os.makedirs(os.path.join(d1, '_internal'), exist_ok=True)
            with open(os.path.join(d1, 'PDD EZ.exe'), 'wb') as f:
                f.write(b'x')
            self.assertTrue(m._is_program_dir(d1), '固定名+_internal 应为 True')
            d2 = os.path.join(tmp, 'Prog2')
            os.makedirs(os.path.join(d2, '_internal'), exist_ok=True)
            with open(os.path.join(d2, 'PDD EZ_v1.4.4.exe'), 'wb') as f:
                f.write(b'x')
            # 版本号 exe（旧版升级目录）分支不得 NameError，应为 True
            self.assertTrue(m._is_program_dir(d2), '版本号 exe+_internal 应为 True')
            d3 = os.path.join(tmp, 'Prog3')
            os.makedirs(d3, exist_ok=True)
            with open(os.path.join(d3, 'PDD EZ Backup.exe'), 'wb') as f:
                f.write(b'x')
            self.assertFalse(m._is_program_dir(d3), '仅有 Backup.exe 无 _internal 应为 False')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_merge_verify_keeps_zero_inventory(self):
        """fix-review N2/C8：真实 0 库存不被副模型覆盖；真空缺才补"""
        import importlib.util, os
        spec = importlib.util.spec_from_file_location('pdd_ocr2', os.path.join(HERE, 'ocr.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        verify = [{'name': 'A', 'stock': '500'}, {'name': 'B', 'stock': '500'}]
        # 触发二次识别：_missing_id=True（缺 ID 主模型行）
        items = [{'name': 'A', 'stock': '0', '_missing_id': True}]
        out = m.merge_verify_items([dict(i) for i in items], [dict(v) for v in verify])
        self.assertEqual(out[0].get('stock'), '0', '0 库存应保留')
        items2 = [{'name': 'B', 'stock': '', '_missing_id': True}]
        out2 = m.merge_verify_items([dict(i) for i in items2], [dict(v) for v in verify])
        self.assertEqual(out2[0].get('stock'), '500', '真空缺应补全')


if __name__ == '__main__':
    unittest.main()
