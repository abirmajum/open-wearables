from __future__ import annotations

import ast


def _id_descendants(node: ast.AST) -> int:
    return sum(isinstance(descendant, ast.Attribute) and descendant.attr == "id" for descendant in ast.walk(node))


def count_resource_id_auth_violations(source: str) -> int:
    tree = ast.parse(source)
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if function_name == "api_key_headers":
                total += sum(_id_descendants(argument) for argument in node.args)
                total += sum(_id_descendants(keyword.value) for keyword in node.keywords)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "X-Open-Wearables-API-Key":
                    total += _id_descendants(value)
    return total
