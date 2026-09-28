"""Normalize public benchmark fields and extract explicit interface paths."""
import json
import re


def requirement_text(value: str) -> str:
    text = (value or "").strip()
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return text
    return decoded.strip() if isinstance(decoded, str) else text


def interface_paths(value: str) -> list:
    paths = []
    for match in re.finditer(r"(?im)^\s*[-*]?\s*(?:\*\*)?Path(?:\*\*)?\s*:\s*(.+)$", requirement_text(value)):
        path = match.group(1).strip().strip("`\"' *")
        for prefix in ("/testbed/", "/app/", "./"):
            if path.startswith(prefix):
                path = path[len(prefix):]
        if path and not path.startswith("/") and ".." not in path.split("/") and not any(c.isspace() for c in path):
            paths.append(path)
    return sorted(set(paths))
