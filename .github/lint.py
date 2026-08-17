#!/usr/bin/env python3
"""CI lint for pcg-agents: validates SKILL.md frontmatter + Python syntax.

Runs in GitHub Actions on every push/PR. Blocks a malformed skill or broken
script from fanning out to every box via fleet sync.
"""
import ast
import os
import re
import sys

errors = []


def check_skill(path):
    text = open(path, encoding="utf-8").read()
    rel = os.path.relpath(path)
    if not text.startswith("---"):
        errors.append(f"{rel}: missing frontmatter (must start with ---)")
        return
    end = text.find("\n---", 3)
    if end == -1:
        errors.append(f"{rel}: unclosed frontmatter")
        return
    fm = text[3:end]
    for key in ("name:", "description:"):
        if not re.search(rf"^{key}", fm, re.M):
            errors.append(f"{rel}: frontmatter missing required '{key}'")
    m = re.search(r"^name:\s*(\S+)", fm, re.M)
    if m and not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,63}", m.group(1)):
        errors.append(f"{rel}: skill name '{m.group(1)}' must be lowercase-hyphenated")


def check_python(path):
    rel = os.path.relpath(path)
    try:
        ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError as e:
        errors.append(f"{rel}: SyntaxError: {e}")


def main():
    for root, _, files in os.walk("."):
        if ".git" in root.split(os.sep):
            continue
        for f in files:
            p = os.path.join(root, f)
            if f == "SKILL.md":
                check_skill(p)
            elif f.endswith(".py") and ".github" not in root:
                check_python(p)
    if errors:
        print("LINT FAILURES:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("lint: all skills and scripts valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
