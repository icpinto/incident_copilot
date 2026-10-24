from gateway import charge, ping


class Primary:
    def retry(self):
        return charge()


class Secondary:
    def retry(self):
        return ping()
