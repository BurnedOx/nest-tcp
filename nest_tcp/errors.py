class RPCException(Exception):
    def __init__(self, error: dict | str):
        if not isinstance(error, dict):
            # NestJS reports some failures (e.g. no handler for the pattern)
            # as a bare string instead of an object.
            error = {'message': error if isinstance(error, str) else str(error)}
        self.code: int = error.get('code', None)
        self.message: str | None = error.get('message', None)
        self.data: dict | None = error.get('data', None)

    def __str__(self):
        message = "Unknown error"
        if self.message:
            message = self.message
        elif isinstance(self.data, dict) and self.data.get('message'):
            message = self.data.get('message')
        return f'RPCException: code={self.code}, message={message}'

    def to_dict(self):
        return {'code': self.code, 'message': self.message, 'data': self.data}
