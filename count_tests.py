#!/usr/bin/env python
"""Count tests per file"""
import subprocess, sys, re

result = subprocess.run(
    [sys.executable, '-m', 'unittest',
     'test_smoke', 'test_algorithm', 'test_algorithm_ui',
     'test_async_queue', 'test_ocr_confidence', 'test_review_flow',
     'test_store_db', 'test_store_ui_logic', 'test_r1_enhanced', '-v'],
    capture_output=True, text=True
)
lines = result.stdout.split('\n')

summary = {}
total_ok = 0
total_fail = 0

for line in lines:
    # Verbose output: test_name (module.Class) ... ok
    # The dots are part of the progress output
    m = re.search(r'^(test_\w+)\s+\(([^)]+)\)\s+\.\.\.\s+(\w+)', line)
    if m:
        test_name = m.group(1)
        module = m.group(2).split('.')[0]  # module.Class -> module
        status = m.group(3).lower()
        if module not in summary:
            summary[module] = {'ok': 0, 'fail': 0}
        if status == 'ok':
            summary[module]['ok'] += 1
            total_ok += 1
        elif status == 'fail':
            summary[module]['fail'] += 1
            total_fail += 1

print('=== Per-file test counts ===')
for mod in sorted(summary.keys()):
    c = summary[mod]
    print(f'{mod}: {c["ok"]} tests (failures={c["fail"]})')
print(f'\nTOTAL: {total_ok} tests OK, {total_fail} failures')

# Also run py_compile
print('\n=== py_compile check ===')
import py_compile, glob, os
bad = []
here = os.path.dirname(os.path.abspath(__file__))
for path in glob.glob(os.path.join(here, '*.py')):
    bn = os.path.basename(path)
    if bn.startswith('test_') or bn.startswith('count_tests'):
        continue
    try:
        py_compile.compile(path, doraise=True)
    except Exception as e:
        bad.append((bn, str(e)))

if bad:
    print(f'FAIL: {bad}')
else:
    print('All non-test .py files compile OK')
