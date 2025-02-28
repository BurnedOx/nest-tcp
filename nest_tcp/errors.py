class RPCException(Exception):
    def __init__(self, error: dict):
        self.code: int = error.get('code', None)
        self.message: str | None = error.get('message', None)
        self.data: dict | None = error.get('data', None)

    def __str__(self):
        message = "Unknown error"
        if self.message:
            message = self.message
        elif self.data and self.data.get('message'):
            message = self.data.get('message')
        return f'RPCException: code={self.code}, message={message}'
