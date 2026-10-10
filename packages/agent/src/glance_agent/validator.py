from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from glance_agent.workspace import load_json


def validate_result(task_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result_path = task_dir / "result.json"
    if not result_path.is_file():
        return {}, [{"path": [], "message": "result.json does not exist"}]
    result = load_json(result_path)
    schema = load_json(task_dir / "result.schema.json")
    validator = Draft202012Validator(schema)
    errors = [
        {"path": list(error.absolute_path), "message": error.message}
        for error in sorted(validator.iter_errors(result), key=lambda item: list(item.path))
    ]
    return result, errors
