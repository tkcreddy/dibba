import logging
import functools
from utils.ReadConfig import ReadConfig as rc
from utils.singleton import Singleton


class LogKCld(metaclass=Singleton):
    read_config = rc()
    logging_config = read_config.logging_config
    log_file = logging_config.get("file_path")
    log_level = logging_config.get("level", logging.INFO)

    # LogRecord reserved attribute names (cannot be overridden via extra=)
    _RESERVED = set(logging.LogRecord(
        name="x", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__.keys())

    def __init__(self, name="dibba", log_file=log_file, level=log_level) -> None:
        # prevent __init__ running multiple times for Singleton
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.logger.propagate = False

        # prevent duplicate handlers if something reloads module
        if getattr(self.logger, "_kcld_handlers_added", False):
            return
        self.logger._kcld_handlers_added = True  # type: ignore[attr-defined]

        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

        print("initializing Logger once")

    def _sanitize_extra(self, extra):
        """
        Ensure `extra` does not contain reserved LogRecord keys like 'args'.
        If reserved keys exist, move them under a safe prefixed name.
        """
        if not extra:
            return None

        if not isinstance(extra, dict):
            # logging requires dict or None
            return {"_extra": str(extra)}

        clean = {}
        for k, v in extra.items():
            if k in self._RESERVED:
                clean[f"_extra_{k}"] = v
            else:
                clean[k] = v
        return clean

    def info(self, msg, extra=None, **kwargs):
        self.logger.info(msg, extra=self._sanitize_extra(extra), **kwargs)

    def error(self, msg, extra=None, **kwargs):
        self.logger.error(msg, extra=self._sanitize_extra(extra), **kwargs)

    def debug(self, msg, extra=None, **kwargs):
        self.logger.debug(msg, extra=self._sanitize_extra(extra), **kwargs)

    # back-compat alias
    def warn(self, msg, extra=None, **kwargs):
        self.logger.warning(msg, extra=self._sanitize_extra(extra), **kwargs)

    def warning(self, msg, *args, **kwargs):
        return self.warn(msg, *args, **kwargs) if hasattr(self, "warn") else self.info(f"WARNING: {msg}")


def log_to_file(logger: LogKCld):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                logger.info(f"Calling Class & function: {func.__name__}")
                logger.info(f"Arguments: {args}, {kwargs}")
                result = func(*args, **kwargs)
                logger.info(f"Function returned: {func.__name__} {result}")
                return result
            except Exception as e:
                # exc_info is valid now because our logger methods accept **kwargs
                logger.error(
                    f"Error in function {func.__name__}: {str(e)}",
                    exc_info=True
                )
                raise
        return wrapper
    return decorator
