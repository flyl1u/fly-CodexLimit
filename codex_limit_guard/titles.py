"""Title changes have an explicit owner; never infer ownership from a prefix."""


def mark_title(current: str, prefix: str) -> tuple[str, bool]:
    if current.startswith(prefix):
        return current, False
    return prefix + current, True


def restore_title(current: str, prefix: str, owned: bool) -> str:
    return current[len(prefix):] if owned and current.startswith(prefix) else current
