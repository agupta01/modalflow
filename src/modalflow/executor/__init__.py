def __getattr__(name):
    if name == "ModalExecutor":
        from modalflow.executor.modal_executor import ModalExecutor

        return ModalExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
