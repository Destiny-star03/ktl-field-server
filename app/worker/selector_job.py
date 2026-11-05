"""Selector를 별도 프로세스에서 호출하는 JSON 입출력 경계."""
from dataclasses import asdict
from datetime import datetime
import json
import sys


def execute(request: dict) -> dict:
    from app.modules import get_selector
    from ktl_contract import SelectResult

    selector = get_selector(request["selector"])
    action, paths = request["action"], request["paths"]
    if action == "detect":
        return selector.detect(paths[0])
    if action == "series":
        frame = selector.series(paths)
        source_units = [source.get("unit") for source in frame.attrs.get("sources", [])]
        return {"columns": frame.columns.tolist(), "rows": frame.values.tolist(),
                "unit": source_units[0] if len(source_units) == 1 else None}
    if action != "run":
        raise ValueError("지원하지 않는 Selector 작업입니다.")
    result = selector.run(paths, request.get("options", {}))
    if not isinstance(result, SelectResult):
        raise TypeError("Selector가 SelectResult를 반환하지 않았습니다.")
    return {"table": {"columns": result.table.columns.tolist(), "rows": result.table.values.tolist()},
            "warnings": [asdict(warning) for warning in result.warnings], "meta": result.meta}


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"JSON으로 표현할 수 없는 타입: {type(value).__name__}")


if __name__ == "__main__":
    try:
        value = execute(json.loads(sys.stdin.read()))
        message = json.dumps({"ok": True, "result": value}, default=json_default, allow_nan=False)
    except Exception as exc:
        message = json.dumps({"ok": False, "error": str(exc)})
    sys.stdout.write(message)
