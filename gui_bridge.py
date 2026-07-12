"""
PDD EZ v2.0 — Bridge loader for compiled .pyc
"""
import marshal, types, os, sys

_pyc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gui.pyc')
if os.path.exists(_pyc_path):
    with open(_pyc_path, 'rb') as _f:
        _f.read(16)
        _code = marshal.load(_f)
    _mod = types.ModuleType('gui')
    exec(_code, _mod.__dict__)
    for _name in dir(_mod):
        if not _name.startswith('_'):
            globals()[_name] = getattr(_mod, _name)
