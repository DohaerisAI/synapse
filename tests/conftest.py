from __future__ import annotations

import asyncio
import inspect


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "asyncio: run test in an asyncio event loop")


def pytest_pyfunc_call(pyfuncitem) -> bool | None:
    test_func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_func):
        return None
    signature = inspect.signature(test_func)
    kwargs = {
        name: value
        for name, value in pyfuncitem.funcargs.items()
        if name in signature.parameters
    }
    asyncio.run(test_func(**kwargs))
    return True
