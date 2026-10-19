"""Gắn các phương thức tách ra từ server.py vào không gian tên của server.

server.py từng là một tệp ~6000 dòng; phần thân ReplyEngine và các nhánh route của
Handler được chuyển NGUYÊN VĂN sang các module nhỏ hơn. Để không đổi hành vi, mọi
tên toàn cục mà mã đó dùng (ROOT, DATA_DIR, clean, report_problem, ...) phải vẫn
được tra trong module ``server`` lúc chạy — đúng như trước khi tách, kể cả khi
test thay bằng ``monkeypatch.setattr(server, "ROOT", ...)``.

``adopt(cls, namespace)`` tạo lại từng hàm của lớp với ``__globals__`` = namespace
(globals() của server). Hàm lồng/lambda bên trong tự kế thừa globals của hàm ngoài.
``rebind(func, namespace)`` làm việc đó cho một hàm rời (bộ xử lý route).
"""
from __future__ import annotations

import types


def rebind(func, namespace):
    if not isinstance(func, types.FunctionType):
        return func
    new = types.FunctionType(func.__code__, namespace, func.__name__,
                             func.__defaults__, func.__closure__)
    new.__kwdefaults__ = func.__kwdefaults__
    new.__dict__.update(func.__dict__)
    new.__doc__ = func.__doc__
    new.__qualname__ = func.__qualname__
    new.__module__ = namespace.get("__name__", func.__module__)
    new.__annotations__ = func.__annotations__
    return new


def adopt(cls, namespace):
    for name, value in list(vars(cls).items()):
        if isinstance(value, types.FunctionType):
            setattr(cls, name, rebind(value, namespace))
        elif isinstance(value, staticmethod):
            setattr(cls, name, staticmethod(rebind(value.__func__, namespace)))
        elif isinstance(value, classmethod):
            setattr(cls, name, classmethod(rebind(value.__func__, namespace)))
        elif isinstance(value, property):
            setattr(cls, name, property(
                rebind(value.fget, namespace) if value.fget else None,
                rebind(value.fset, namespace) if value.fset else None,
                rebind(value.fdel, namespace) if value.fdel else None,
                value.__doc__))
    return cls
