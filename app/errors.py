class DomainError(ValueError):
    def __init__(self, code, message, status=422, details=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}

    def body(self):
        result = {'code': self.code, 'message': self.message}
        if self.details:
            result.update(self.details)
        return result
