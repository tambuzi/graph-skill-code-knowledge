"""DB module fixture."""


# Opens the connection pool
def connect():
    return open_pool()


def open_pool():
    return {"pool": True}
