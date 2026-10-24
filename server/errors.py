class Problem(Exception):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status
        super().__init__(code)
