"""Strip C comments from ADDED ('+') lines of a unified diff (Chenxi's exec-rl
strip_comments.py, refactored to expose strip_diff() for import while keeping the
CLI). Only '+' body lines are touched: a comment-only added line becomes a clean
added blank line; an inline trailing comment is removed but the code is kept.
Context and '-' lines are left byte-for-byte identical so the patch still applies."""

import re
import sys


def strip_added(line: str) -> str:
    # line starts with '+', not '+++'; `line` is expected without a trailing newline.
    body = line[1:]
    b = re.sub(r'/\*.*?\*/', '', body)          # inline block comments
    b = re.sub(r'/\*.*$', '', b)                # block start, no end on this line
    if re.match(r'^\s*\*', b) or re.match(r'^\s*\*/', body):  # block continuation / end
        b = ''
    b = re.sub(r'//.*$', '', b)                 # line comments
    if b.strip() == '':
        return '+'
    return '+' + b.rstrip() + '\n' if not b.endswith('\n') else '+' + b


def strip_diff(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith('+') and not line.startswith('+++'):
            stripped = strip_added(line.rstrip('\n'))
            out.append(stripped if stripped.endswith('\n') else stripped + '\n')
        else:
            out.append(line)
    return ''.join(out)


if __name__ == "__main__":
    sys.stdout.write(strip_diff(sys.stdin.read()))
