"""DB module fixture."""


def connect():
    return open_pool()


def open_pool():
    return {"pool": True}
