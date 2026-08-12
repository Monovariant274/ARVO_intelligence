"""Crash type taxonomy for kernel bug prediction."""

from typing import Literal

CrashType = Literal[
    "kasan",
    "kfence",
    "kmsan",
    "ubsan",
    "warning",
    "bug",
    "null_deref",
    "gpf",
    "panic",
    "deadlock",
    "hung_task",
    "memory_leak",
    "divide_error",
    "stack_overflow",
    "other",
]

CRASH_TYPE_DESCRIPTIONS: dict[str, str] = {
    "kasan": "KASAN: memory error (use-after-free, out-of-bounds, slab corruption, etc.)",
    "kfence": "KFENCE: lightweight memory safety error detector",
    "kmsan": "KMSAN: use of uninitialized memory",
    "ubsan": "UBSAN: undefined behavior (integer overflow, shift, etc.)",
    "warning": "Kernel WARNING (WARN / WARN_ON assertion fired)",
    "bug": "Kernel BUG (BUG_ON or explicit BUG() assertion)",
    "null_deref": "NULL pointer dereference not caught by KASAN",
    "gpf": "General protection fault (bad memory address in kernel mode)",
    "panic": "Kernel panic (unrecoverable error)",
    "deadlock": "Deadlock or lockdep circular dependency detected",
    "hung_task": "Task hung / soft lockup detected",
    "memory_leak": "Memory leak detected (kmemleak or similar)",
    "divide_error": "Divide by zero or similar arithmetic fault",
    "stack_overflow": "Kernel stack overflow",
    "other": "Other / unclassified crash type",
}

_TITLE_PREFIXES: list[tuple[str, CrashType]] = [
    ("KASAN:", "kasan"),
    ("KFENCE:", "kfence"),
    ("KMSAN:", "kmsan"),
    ("UBSAN:", "ubsan"),
    ("WARNING IN", "warning"),
    ("WARNING:", "warning"),
    ("WARN IN", "warning"),
    ("KERNEL BUG", "bug"),  # covers both "kernel BUG at <addr>" (oops) and "kernel BUG in <func>" (syzbot)
    ("BUG: UNABLE TO HANDLE KERNEL NULL POINTER", "null_deref"),
    ("BUG: KERNEL NULL POINTER DEREFERENCE", "null_deref"),
    ("BUG: NULL POINTER DEREFERENCE", "null_deref"),
    ("BUG:", "bug"),
    ("GENERAL PROTECTION FAULT", "gpf"),
    ("STACK SEGMENT FAULT", "gpf"),
    ("INFO: RCU DETECTED STALL", "hung_task"),
    ("INFO: TRYING TO REGISTER NON-STATIC KEY", "deadlock"),
    ("KERNEL PANIC", "panic"),
    ("POSSIBLE DEADLOCK", "deadlock"),
    ("LOCKDEP:", "deadlock"),
    ("INFO: TASK", "hung_task"),
    ("SOFT LOCKUP", "hung_task"),
    ("MEMORY LEAK", "memory_leak"),
    ("KMEMLEAK:", "memory_leak"),
    ("DIVIDE ERROR", "divide_error"),
    ("DIVIDE BY ZERO", "divide_error"),
    ("STACK-OUT-OF-BOUNDS", "kasan"),
    ("STACK OVERFLOW", "stack_overflow"),
]


def classify_crash_title(title: str) -> CrashType:
    """Map a syzbot bug title to its CrashType.

    Used for ground-truth extraction at analysis time.
    """
    upper = title.upper()
    for prefix, crash_type in _TITLE_PREFIXES:
        if upper.startswith(prefix):
            return crash_type
    # Fallback: substring match for sanitizer names that may appear mid-title
    for keyword, crash_type in [
        ("KASAN", "kasan"),
        ("KFENCE", "kfence"),
        ("KMSAN", "kmsan"),
        ("UBSAN", "ubsan"),
    ]:
        if keyword in upper:
            return crash_type
    return "other"
