def invoke(target):
    return getattr(target, "charge")()
