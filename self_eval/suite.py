"""Versioned questions and deterministic checks. Answers never enter Agent workspaces."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import ntpath
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUITE_VERSION = "praxis-self-eval-20-v1"
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class Check:
    kind: str
    path: str
    expected: Any = None


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    question: str
    files: dict[str, str]
    checks: tuple[Check, ...]
    mutable: frozenset[str] = field(default_factory=frozenset)
    require_command: bool = False
    split: str = "holdout"

    @property
    def prompt(self) -> str:
        return (
            f"自建任务 {self.id}\n{self.question}\n\n"
            "请实际调用工具完成任务，不要只描述方案。所有路径均相对于当前工作目录；"
            "仅操作当前工作目录中的测试文件，不要访问外部文件、网络、安装依赖或绕过沙箱。"
            "除题目明确要求修改或移动的文件外，保留所有输入文件。"
            "创建输出所需的父目录。输出使用UTF-8编码。JSON数值使用数字类型，路径使用正斜杠。"
            "可使用当前终端或标准库，不需要向用户提问。完成后简短汇报。"
        )


def safe_path(root: Path, relative: str) -> Path:
    """Reject Windows drives, traversal and all symlink/junction components."""
    root = root.absolute()
    for component in (*reversed(root.parents), root):
        if component.is_symlink() or (
            component.exists()
            and getattr(component.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("Workspace root cannot contain symlinks or reparse points")
    root = root.resolve()
    if not relative or ntpath.isabs(relative) or ntpath.splitdrive(relative)[0]:
        raise ValueError("Only workspace-relative paths are allowed")
    parts = relative.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError("Invalid workspace path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symbolic links are not allowed")
        if path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Reparse points are not allowed")
    resolved = path.resolve()
    if root not in resolved.parents:
        raise ValueError("Path escapes workspace")
    return path


def seed_task(task: Task, workspace: Path) -> None:
    """Only seed a brand-new directory; never reset or delete user files."""
    workspace.mkdir(parents=True, exist_ok=False)
    for relative, content in task.files.items():
        path = safe_path(workspace, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))


def read_text(workspace: Path, relative: str) -> str:
    path = safe_path(workspace, relative)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Missing, invalid or oversized output")
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")


def json_equal(actual: Any, expected: Any) -> bool:
    """Numeric tolerance without accepting strings, bools or non-finite values."""
    if isinstance(expected, bool) or expected is None:
        return actual is expected
    if isinstance(expected, int | float):
        return (
            isinstance(actual, int | float)
            and not isinstance(actual, bool)
            and math.isfinite(actual)
            and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-6)
        )
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(json_equal(actual[key], value) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(json_equal(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return type(actual) is type(expected) and actual == expected


def grade_task(task: Task, workspace: Path, successful_tools: set[str]) -> list[dict]:
    """Read-only grading; no execution of generated scripts or model-based judging."""
    verdicts = []
    for check in task.checks:
        try:
            if check.kind == "absent":
                passed = not safe_path(workspace, check.path).exists()
            else:
                text = read_text(workspace, check.path)
                if check.kind == "json":
                    passed = json_equal(json.loads(text), check.expected)
                elif check.kind == "text":
                    passed = text.rstrip("\n") == check.expected.rstrip("\n")
                elif check.kind == "csv":
                    passed = list(csv.reader(io.StringIO(text))) == check.expected
                else:
                    raise ValueError("Unknown checker")
            verdicts.append({"check": f"{check.kind}:{check.path}", "passed": passed})
        except (OSError, ValueError, TypeError, OverflowError):
            verdicts.append(
                {"check": f"{check.kind}:{check.path}", "passed": False, "reason": "missing_or_invalid_output"}
            )
    for relative, content in task.files.items():
        if relative in task.mutable:
            continue
        try:
            path = safe_path(workspace, relative)
            passed = (
                path.is_file()
                and path.stat().st_size <= MAX_FILE_BYTES
                and path.read_bytes() == content.encode("utf-8")
            )
        except (OSError, ValueError):
            passed = False
        verdicts.append({"check": f"preserved:{relative}", "passed": passed})
    verdicts.append({"check": "actual_tool_execution", "passed": bool(successful_tools)})
    if task.require_command:
        verdicts.append({"check": "successful_run_command", "passed": "run_command" in successful_tools})
    return verdicts


def _j(path: str, expected: Any) -> Check:
    return Check("json", path, expected)


_inventory = {"input/中文.txt": "你好\n", "input/sub/a.txt": "abc\n", "input/empty.txt": ""}
TASKS = (
    Task(
        "cmd-01",
        "command",
        '通过命令工具计算1到100的整数和，写入result.json，格式为{"sum":数字}。',
        {},
        (_j("result.json", {"sum": 5050}),),
        require_command=True,
        split="dev",
    ),
    Task(
        "cmd-02",
        "command",
        '通过命令工具找出2到30（含端点）的所有素数，升序写入result.json，格式为{"primes":[数字,...]}。',
        {},
        (_j("result.json", {"primes": [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]}),),
        require_command=True,
        split="dev",
    ),
    Task(
        "cmd-03",
        "command",
        '通过命令工具递归统计input目录中的普通文件和子目录数量，子目录数不包括input本身。写入result.json，格式为{"files":数字,"directories":数字}。',
        {
            "input/a.txt": "a",
            "input/中文.txt": "你好",
            "input/a/1.txt": "1",
            "input/b/2.txt": "2",
            "input/b/deep/3.txt": "3",
        },
        (_j("result.json", {"files": 5, "directories": 3}),),
        require_command=True,
    ),
    Task(
        "cmd-04",
        "command",
        "通过命令工具统计input下普通文件的扩展名，忽略大小写，无扩展名计入other。写入result.json，键必须为txt、csv、json、other，值为数量。",
        {"input/a.txt": "a", "input/b.TXT": "b", "input/c.csv": "x\n1\n", "input/d.json": "{}", "input/README": "说明"},
        (_j("result.json", {"txt": 2, "csv": 1, "json": 1, "other": 1}),),
        require_command=True,
    ),
    Task(
        "cmd-05",
        "command",
        '通过命令工具递归查找input下文件名以report_开头且扩展名为.txt的文件；按路径字典序排列，路径相对于当前工作目录。写入result.json，格式为{"paths":[路径,...]}。',
        {
            "input/a/report_01.txt": "1",
            "input/b/report_02.txt": "2",
            "input/report_old.csv": "old",
            "input/b/note.txt": "note",
        },
        (_j("result.json", {"paths": ["input/a/report_01.txt", "input/b/report_02.txt"]}),),
        require_command=True,
    ),
    Task(
        "file-01",
        "file",
        "将config/app.json的port修改为9000，保留其他字段和值，不新增字段。",
        {"config/app.json": '{"port":8000,"debug":false,"service":"demo","features":["search","files"]}\n'},
        (_j("config/app.json", {"port": 9000, "debug": False, "service": "demo", "features": ["search", "files"]}),),
        mutable=frozenset({"config/app.json"}),
        split="dev",
    ),
    Task(
        "file-02",
        "file",
        "创建output/你好.txt，内容为一行：你好，Praxis！允许末尾换行。",
        {},
        (Check("text", "output/你好.txt", "你好，Praxis！"),),
        split="dev",
    ),
    Task(
        "file-03",
        "file",
        "将inbox下的所有.txt文件在原目录重命名：文件名前加report_，保留内容，旧文件不再存在；不要改动.md文件。",
        {"inbox/one.txt": "one\n", "inbox/two.txt": "two\n", "inbox/keep.md": "keep\n"},
        (
            Check("text", "inbox/report_one.txt", "one\n"),
            Check("text", "inbox/report_two.txt", "two\n"),
            Check("absent", "inbox/one.txt"),
            Check("absent", "inbox/two.txt"),
        ),
        mutable=frozenset({"inbox/one.txt", "inbox/two.txt"}),
    ),
    Task(
        "file-04",
        "file",
        "按a.txt、b.txt顺序合并pieces中的文本到output/merged.txt，每段独占一行，不加入标题或空行。",
        {"pieces/b.txt": "第二段\n", "pieces/a.txt": "第一段\n"},
        (Check("text", "output/merged.txt", "第一段\n第二段\n"),),
    ),
    Task(
        "file-05",
        "file",
        "将docs目录完整复制到backup/docs，保留相对目录结构和文件内容，源文件不变。",
        {"docs/a.txt": "alpha\n", "docs/sub/中文.txt": "资料\n"},
        (Check("text", "backup/docs/a.txt", "alpha\n"), Check("text", "backup/docs/sub/中文.txt", "资料\n")),
    ),
    Task(
        "data-01",
        "data",
        "读取orders.csv，按product汇总price乘quantity所得销售额。写入summary.json，商品名为键，销售额为数字，保留零销售额商品。",
        {"orders.csv": "product,price,quantity\nbook,19.90,2\npen,2.50,3\npen,5.00,1\nnotebook,10.00,0\n"},
        (_j("summary.json", {"book": 39.8, "pen": 12.5, "notebook": 0}),),
        split="dev",
    ),
    Task(
        "data-02",
        "data",
        "读取users.csv，按id去重，同一id只保留最后出现的一行，按id升序写入users.json数组，每项仅包含id、name、age；id和age为数字。",
        {"users.csv": "id,name,age\n2,李四,21\n1,张三,20\n2,李四,22\n3,王五,23\n"},
        (
            _j(
                "users.json",
                [
                    {"id": 1, "name": "张三", "age": 20},
                    {"id": 2, "name": "李四", "age": 22},
                    {"id": 3, "name": "王五", "age": 23},
                ],
            ),
        ),
        split="dev",
    ),
    Task(
        "data-03",
        "data",
        "统计app.log每行开头的[INFO]、[WARNING]、[ERROR]日志数量，只统计行首级别，不统计正文中出现的词或[NOTERROR]。写入counts.json，键为INFO、WARNING、ERROR。",
        {
            "app.log": "[INFO] start\n[ERROR] failed\n[WARNING] retry ERROR\n[INFO] recovered\n[ERROR] stop\n[NOTERROR] ignore\n"
        },
        (_j("counts.json", {"INFO": 2, "WARNING": 1, "ERROR": 2}),),
    ),
    Task(
        "data-04",
        "data",
        "读取scores.json，按score降序排序，同分时按name字典序升序，保持每条记录原有字段，写入sorted.json。",
        {"scores.json": '[{"name":"Bob","score":90},{"name":"Alice","score":90},{"name":"Chen","score":75}]\n'},
        (
            _j(
                "sorted.json",
                [{"name": "Alice", "score": 90}, {"name": "Bob", "score": 90}, {"name": "Chen", "score": 75}],
            ),
        ),
    ),
    Task(
        "data-05",
        "data",
        '读取empty.csv，统计数据行数量和amount合计，表头不是数据行；没有数据时合计为0。写入summary.json，格式为{"rows":数字,"total":数字}。',
        {"empty.csv": "id,amount\n"},
        (_j("summary.json", {"rows": 0, "total": 0}),),
    ),
    Task(
        "multi-01",
        "workflow",
        "读取sales.csv，删除price或quantity不是数字、为空或小于0的行；保留合法行原顺序和文本字段。生成clean.csv（表头id,price,quantity）以及summary.json，键为kept、rejected、total，分别为保留行数、删除行数、合法行price乘quantity之和。",
        {"sales.csv": "id,price,quantity\na,10,2\nb,,1\nc,3,-1\nd,2.5,1\ne,oops,2\n"},
        (
            Check("csv", "clean.csv", [["id", "price", "quantity"], ["a", "10", "2"], ["d", "2.5", "1"]]),
            _j("summary.json", {"kept": 2, "rejected": 3, "total": 22.5}),
        ),
        split="dev",
    ),
    Task(
        "multi-02",
        "workflow",
        "将downloads中的.txt、.csv、.json文件分别移动到organized/text、organized/tabular、organized/json；保持文件名和内容，原文件不再存在，其他文件不动。再生成inventory.json，键为text、tabular、json，值为各目标目录中文件数量。",
        {
            "downloads/a.txt": "A\n",
            "downloads/c.txt": "C\n",
            "downloads/b.csv": "id\n1\n",
            "downloads/d.json": "{}\n",
            "downloads/keep.bin": "keep",
        },
        (
            Check("text", "organized/text/a.txt", "A\n"),
            Check("text", "organized/text/c.txt", "C\n"),
            Check("text", "organized/tabular/b.csv", "id\n1\n"),
            Check("text", "organized/json/d.json", "{}\n"),
            *tuple(Check("absent", f"downloads/{name}") for name in ("a.txt", "b.csv", "c.txt", "d.json")),
            _j("inventory.json", {"text": 2, "tabular": 1, "json": 1}),
        ),
        mutable=frozenset(f"downloads/{name}" for name in ("a.txt", "b.csv", "c.txt", "d.json")),
        split="dev",
    ),
    Task(
        "multi-03",
        "workflow",
        '提取notes.md中未完成的Markdown待办项（- [ ]），按原顺序生成todos.json，格式为{"count":数字,"items":[待办正文,...]}；忽略已完成项及普通列表项。另生成summary.txt，一行内容为待办数量：N，N替换为数量。',
        {"notes.md": "# 计划\n- [ ] 阅读代码\n- [x] 安装环境\n- 普通条目\n- [ ] 完成测试\n"},
        (
            _j("todos.json", {"count": 2, "items": ["阅读代码", "完成测试"]}),
            Check("text", "summary.txt", "待办数量：2"),
        ),
    ),
    Task(
        "multi-04",
        "workflow",
        "递归读取input中的所有普通文件，为每个文件计算UTF-8原始文件字节数及SHA-256十六进制摘要（小写）。生成manifest.json数组，每项仅包含path、bytes、sha256，path相对于工作目录，按path字典序排列，包含空文件。",
        _inventory,
        (
            _j(
                "manifest.json",
                [
                    {
                        "path": path,
                        "bytes": len(content.encode("utf-8")),
                        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    }
                    for path, content in sorted(_inventory.items())
                ],
            ),
        ),
    ),
    Task(
        "multi-05",
        "workflow",
        '读取config.json并校验：port必须是1到65535的整数（字符串无效），retries必须是0到5的整数，timeout必须是正数。不合法时分别替换为8080、2、30，合法字段及其他字段保持不变。生成corrected.json，不修改原文件；另生成report.json，格式为{"changed":[修改的字段名,...],"count":数字}，字段名按字典序排列。',
        {"config.json": '{"port":"bad","retries":-1,"timeout":20,"name":"demo"}\n'},
        (
            _j("corrected.json", {"port": 8080, "retries": 2, "timeout": 20, "name": "demo"}),
            _j("report.json", {"changed": ["port", "retries"], "count": 2}),
        ),
    ),
)


def suite_digest(tasks: tuple[Task, ...] = TASKS) -> str:
    from dataclasses import asdict

    payload = [{**asdict(task), "mutable": sorted(task.mutable)} for task in tasks]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def select_tasks(ids: list[str] | None = None, split: str = "all") -> tuple[Task, ...]:
    known = {task.id for task in TASKS}
    if ids and set(ids) - known:
        raise ValueError("Unknown task IDs: " + ", ".join(sorted(set(ids) - known)))
    selected = tuple(task for task in TASKS if (not ids or task.id in ids) and (split == "all" or task.split == split))
    if not selected:
        raise ValueError("No tasks selected")
    return selected
