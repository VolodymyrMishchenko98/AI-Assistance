class ConcurrencyError(RuntimeError):
    """The write lock could not be acquired within the busy timeout."""