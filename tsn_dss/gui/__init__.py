__all__ = ["create_http_server", "run_server"]


def __getattr__(name: str):
    if name in __all__:
        from .http_api import create_http_server, run_server

        return {"create_http_server": create_http_server, "run_server": run_server}[name]
    raise AttributeError(name)
