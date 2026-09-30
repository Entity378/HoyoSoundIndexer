# Helpers for game tables whose field names are obfuscated, so values are found by walking everything.

def walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from walk_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_strings(value)


# HSR wraps its text hashes as {"Hash": n}, so nested integers count too.
def walk_scalars(node):
    if isinstance(node, (str, int)) and not isinstance(node, bool):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from walk_scalars(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_scalars(value)


# ZZZ wraps its rows in a single top-level key, other tables are lists or keyed by id.
def table_rows(doc):
    if isinstance(doc, dict):
        first = next(iter(doc.values()), None)
        rows = first if isinstance(first, list) else list(doc.values())
    else:
        rows = doc
    return [row for row in rows or [] if isinstance(row, dict)]
