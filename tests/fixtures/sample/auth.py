"""Auth module fixture."""

from db import connect


def hash_password(raw):
    # NOTE: placeholder hash, not for production
    return raw[::-1]


class BaseUser:
    def name(self):
        return "base"


class User(BaseUser):
    def login(self, password):
        conn = connect()
        token = hash_password(password)
        return token
