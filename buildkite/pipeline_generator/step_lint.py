"""The lint of `devices:` steps (step_vendors.py), with `--fix`.

Besides the load errors (E1-E9, which only report), a step is checked for:

  * L1 hoist: with two or more devices, a software value every device's
    resolved entry has (a dependency, an env var with its value, the same
    commands, working_dir, automatic_shard, ...) belongs at the step level;
  * L2: a device whose written fields equal an earlier same-vendor sibling's
    is written `same_as: <sibling>`;
  * L3 (a warning, never fixed): a dependency already covered by a broader
    one at the same or a shared scope (`vllm/models/x/` under a step-level
    `vllm/`). Dropping it changes no run decision, but a reviewer decides;
  * L4 order: a step is label, key, devices, then software fields, then the
    other step fields; devices are NVIDIA, then AMD, then CPU, each vendor in
    its written order; a device is same_as, key, label, machine fields,
    software fields, then variants.

`--fix` rewrites the yaml text in place, keeping comments with the lines they
sit above: values are moved as text, not re-emitted. When a value is hoisted
from several devices, the comments of the copies that are deleted go above
the moved item, or above the step-level field (a comment above a device field
the move empties, say); a comment written the same twice is kept once. A
comment inside a field `same_as` replaces is kept above `same_as`. A fix that
would still drop a comment is refused. A fix never changes a
job: every fixed step is compiled before and after, and a step whose jobs
would differ (a key, say) is left alone and reported. When the fixed order
would change a default key (a CPU device moved after an NVIDIA one), the old
key is pinned with `key:`.
"""

from __future__ import annotations

import copy
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from step import compile_device_jobs
from step_vendors import (
    DEPENDENCIES_FIELD,
    DEVICE_ORDER,
    DEVICES,
    DEVICES_FIELD,
    ENV_FIELD,
    MIRROR_HARDWARE,
    SAME_AS_FIELD,
    SET_ONCE_FIELDS,
    SOFTWARE_FIELDS,
    STEP_ORDER,
    VARIANT_ORDER,
    VARIANTS_FIELD,
    VENDOR_ORDER,
    DeviceStepError,
    compile_device_step,
    default_keys,
    expand_steps,
    flow_style_nodes,
    is_device_step,
    resolve_step,
)

IDENTITY = (SAME_AS_FIELD, "key", "label", VARIANTS_FIELD)


@dataclass
class Finding:
    path: str
    line: int
    code: str
    message: str
    fixable: bool = False

    @property
    def is_error(self) -> bool:
        return self.code.startswith("E") or self.code in ("L1", "L2", "L4")

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"


# ---- the checks --------------------------------------------------------------------


def lint_text(text: str, path: str = "<yaml>") -> List[Finding]:
    """Every finding of one job file."""
    findings: List[Finding] = []
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return [Finding(path, 1, "E2", f"not valid yaml: {error}")]
    steps = doc.get("steps") if isinstance(doc, dict) else None
    if not isinstance(steps, list):
        return findings
    lines = _step_lines(text)
    for line, where in flow_style_nodes(text):
        findings.append(
            Finding(
                path,
                line,
                "E9",
                f"flow-style list or map{where}: write lists with `- ` items "
                "and maps with one key per line",
            )
        )
    group_depends_on = doc.get("depends_on")
    for index, raw in enumerate(steps):
        line = lines[index] if index < len(lines) else 1
        if not is_device_step(raw):
            try:
                expand_steps([raw])
            except DeviceStepError as error:
                findings.append(Finding(path, line, error.code, _message(error)))
            continue
        try:
            # As the loader does: every job checked by the Step model too.
            compile_device_jobs(raw, group_depends_on)
        except DeviceStepError as error:
            findings.append(Finding(path, line, error.code, _message(error)))
            continue
        findings.extend(
            Finding(path, line, code, message, fixable=code != "L3")
            for code, message in step_findings(raw)
        )
    return findings


def lint_keys(texts: Dict[str, str]) -> List[Finding]:
    """E8 across files: a `devices:` job's key that another job also has."""
    owners: Dict[str, List[Tuple[str, bool]]] = {}
    for path, text in texts.items():
        try:
            doc = yaml.safe_load(text)
            steps = doc.get("steps") if isinstance(doc, dict) else None
            if not isinstance(steps, list):
                continue
            for raw in steps:
                device_step = is_device_step(raw)
                compiled = (
                    compile_device_step(raw, doc.get("depends_on"))
                    if device_step
                    else [raw]
                )
                for step in compiled:
                    if not isinstance(step, dict) or not step.get("key"):
                        continue
                    owners.setdefault(step["key"], []).append((path, device_step))
                    if (step.get("mirror") or {}).get(MIRROR_HARDWARE):
                        owners.setdefault(f"amd-{step['key']}", []).append(
                            (path, device_step)
                        )
        except (yaml.YAMLError, DeviceStepError):
            continue  # reported per file
    findings = []
    for key, where in sorted(owners.items()):
        if len(where) > 1 and any(device_step for _, device_step in where):
            files = sorted({path for path, _ in where})
            findings.append(
                Finding(
                    files[0],
                    1,
                    "E8",
                    f"duplicate CI step key {key} (a `devices:` step's job), in "
                    + ", ".join(files),
                )
            )
    return findings


def step_findings(raw: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(code, message) of every lint finding of a valid `devices:` step."""
    key = raw["key"]
    findings: List[Tuple[str, str]] = []
    entries = _entries(raw)
    for target, devices in _same_as_candidates(entries):
        for device in devices:
            findings.append(
                (
                    "L2",
                    f"step {key}, device {device}: written the same as {target}; "
                    f"write `same_as: {target}`",
                )
            )
    for name, value in _hoistable(raw):
        what = f"`{name}`" if value is None else f"`{name}` {value!r}"
        findings.append(
            (
                "L1",
                f"step {key}: every device has {what}; set it once at the step level",
            )
        )
    for message in _covered_dependencies(raw):
        findings.append(("L3", f"step {key}: {message}"))
    for message in _order_problems(raw):
        findings.append(("L4", f"step {key}: {message}"))
    return findings


def _entries(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        device: dict(entry or {})
        for device, entry in (raw.get(DEVICES_FIELD) or {}).items()
    }


def _written(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {name: value for name, value in entry.items() if name not in IDENTITY}


def _same_as_candidates(
    entries: Dict[str, Dict[str, Any]],
) -> List[Tuple[str, List[str]]]:
    """L2: (sibling, devices written the same as it)."""
    candidates: Dict[str, List[str]] = {}
    names = list(entries)
    for index, device in enumerate(names):
        entry = entries[device]
        written = _written(entry)
        if SAME_AS_FIELD in entry or not written:
            continue
        for sibling in names[:index]:
            if (
                DEVICES[sibling].vendor == DEVICES[device].vendor
                and SAME_AS_FIELD not in entries[sibling]
                and _written(entries[sibling]) == written
            ):
                candidates.setdefault(sibling, []).append(device)
                break
    return list(candidates.items())


def _resolved_entries(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Each device's own software fields, with `same_as` applied."""
    entries = _entries(raw)
    resolved = {}
    for device, entry in entries.items():
        own = {
            name: copy.deepcopy(value)
            for name, value in _written(entry).items()
            if name in SOFTWARE_FIELDS
        }
        sibling = entry.get(SAME_AS_FIELD)
        if sibling:
            base = {
                name: copy.deepcopy(value)
                for name, value in _written(entries[sibling]).items()
                if name in SOFTWARE_FIELDS
            }
            for name, value in own.items():
                if name == DEPENDENCIES_FIELD:
                    base[name] = list(base.get(name) or []) + list(value)
                elif name == ENV_FIELD:
                    base[name] = {**(base.get(name) or {}), **value}
                else:
                    base[name] = value
            own = base
        resolved[device] = own
    return resolved


def _hoistable(raw: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """L1: (field, value) every device has; the value is a dependency, an
    (env var, value) pair, or None for a whole field."""
    resolved = _resolved_entries(raw)
    if len(resolved) < 2:
        return []
    entries = list(resolved.values())
    found: List[Tuple[str, Any]] = []
    for dep in entries[0].get(DEPENDENCIES_FIELD) or []:
        if all(dep in (entry.get(DEPENDENCIES_FIELD) or []) for entry in entries):
            found.append((DEPENDENCIES_FIELD, dep))
    for var, value in (entries[0].get(ENV_FIELD) or {}).items():
        if all(
            (entry.get(ENV_FIELD) or {}).get(var, object()) == value
            for entry in entries
        ):
            found.append((ENV_FIELD, (var, value)))
    for name in SET_ONCE_FIELDS:
        if name in entries[0] and all(
            name in entry and entry[name] == entries[0][name] for entry in entries
        ):
            found.append((name, None))
    return found


def _covers(broad: str, narrow: str) -> bool:
    broad, narrow = broad.rstrip("/"), narrow.rstrip("/")
    return bool(broad) and narrow.startswith(broad + "/")


def _covered_dependencies(raw: Dict[str, Any]) -> List[str]:
    """L3: an include covered by a broader include of the same or a shared
    scope (exclusions apply to every include alike, so they do not matter)."""
    shared = [d for d in raw.get(DEPENDENCIES_FIELD) or [] if not d.startswith("!")]
    messages = []

    def check(scope: str, own: List[str], wider: List[str]) -> None:
        for dep in own:
            if dep.startswith("!"):
                continue
            broader = next(
                (
                    other
                    for other in wider + own
                    if other != dep and _covers(other, dep)
                ),
                None,
            )
            if broader:
                messages.append(f"{scope}`{dep}` is already covered by `{broader}`")

    check("", shared, [])
    for device, entry in _entries(raw).items():
        own = [d for d in entry.get(DEPENDENCIES_FIELD) or [] if not d.startswith("!")]
        check(f"device {device}: ", own, shared)
        for name, body in (entry.get(VARIANTS_FIELD) or {}).items():
            extra = [
                d
                for d in (body or {}).get(DEPENDENCIES_FIELD) or []
                if not d.startswith("!")
            ]
            check(f"device {device}, variant {name}: ", extra, shared + own)
    return messages


def _rank(order: Sequence[str], name: str) -> int:
    return order.index(name) if name in order else len(order)


def _out_of_order(names: List[str], order: Sequence[str]) -> bool:
    ranks = [_rank(order, name) for name in names]
    return ranks != sorted(ranks)


def _device_rank(device: str) -> int:
    return VENDOR_ORDER.index(DEVICES[device].vendor)


def _order_problems(raw: Dict[str, Any]) -> List[str]:
    problems = []
    if _out_of_order(list(raw), STEP_ORDER):
        problems.append(
            "fields out of order (label, key, devices, software fields, then "
            "the other step fields)"
        )
    devices = list(raw.get(DEVICES_FIELD) or {})
    if [_device_rank(d) for d in devices] != sorted(_device_rank(d) for d in devices):
        problems.append("devices out of order (NVIDIA, then AMD, then CPU)")
    for device, entry in _entries(raw).items():
        if _out_of_order(list(entry), DEVICE_ORDER + (VARIANTS_FIELD,)):
            problems.append(
                f"device {device}: fields out of order (same_as, key, label, "
                "machine fields, software fields, variants)"
            )
        for name, body in (entry.get(VARIANTS_FIELD) or {}).items():
            if _out_of_order(list(body or {}), VARIANT_ORDER):
                problems.append(
                    f"device {device}, variant {name}: fields out of order (key, "
                    "label, machine fields, software fields)"
                )
    return problems


def _message(error: DeviceStepError) -> str:
    return f"{error.where}: {error.detail}"


def _step_lines(text: str) -> List[int]:
    """The 1-based first line of every step."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return []
    if not isinstance(root, yaml.MappingNode):
        return []
    for key_node, value_node in root.value:
        if key_node.value == "steps" and isinstance(value_node, yaml.SequenceNode):
            return [item.start_mark.line + 1 for item in value_node.value]
    return []


# ---- the text model --------------------------------------------------------------
#
# A block-style yaml mapping as entries, each the comment lines above it plus its
# key line and every deeper line under it. Moving an entry moves its comments.


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_comment_or_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


@dataclass
class Entry:
    key: str
    lead: List[str]
    lines: List[str]

    def text(self) -> List[str]:
        return self.lead + self.lines


@dataclass
class Block:
    """A mapping's entries at one indent, and comment lines after the last."""

    indent: int
    entries: List[Entry]
    trailing: List[str] = field(default_factory=list)

    def get(self, key: str) -> Optional[Entry]:
        return next((entry for entry in self.entries if entry.key == key), None)

    def lines(self) -> List[str]:
        out: List[str] = []
        for entry in self.entries:
            out.extend(entry.text())
        return out + self.trailing


_KEY_LINE = re.compile(r"^\s*(\"[^\"]*\"|'[^']*'|[^\s:#][^:#]*?)\s*:(\s|$)")


def _key_of(line: str) -> str:
    match = _KEY_LINE.match(line)
    if not match:
        raise ValueError(f"not a `key:` line: {line!r}")
    key = match.group(1)
    if key[:1] in "\"'":
        key = key[1:-1]
    return key


def parse_block(lines: List[str], indent: int) -> Block:
    entries: List[Entry] = []
    pending: List[str] = []
    for line in lines:
        if _is_comment_or_blank(line):
            pending.append(line)
            continue
        if _indent(line) == indent and not line.lstrip().startswith("- "):
            entries.append(Entry(_key_of(line), pending, [line]))
        else:
            if not entries:
                raise ValueError(f"unexpected line: {line!r}")
            entries[-1].lines.extend(pending)
            entries[-1].lines.append(line)
        pending = []
    return Block(indent, entries, pending)


def _child_indent(entry: Entry) -> Optional[int]:
    for line in entry.lines[1:]:
        if not _is_comment_or_blank(line):
            return _indent(line)
    return None


def child_block(entry: Entry, default: int) -> Block:
    """The mapping under an entry (empty for `key:` with nothing under it)."""
    indent = _child_indent(entry)
    return parse_block(entry.lines[1:], default if indent is None else indent)


def set_child_block(entry: Entry, block: Block) -> None:
    entry.lines = [entry.lines[0]] + block.lines()


def _items(entry: Entry) -> Tuple[int, List[Entry], List[str]]:
    """A list entry's items (each `- ` line and the deeper lines under it)."""
    indent = _child_indent(entry)
    items: List[Entry] = []
    pending: List[str] = []
    for line in entry.lines[1:]:
        if _is_comment_or_blank(line):
            pending.append(line)
            continue
        if _indent(line) == indent and line.lstrip().startswith("- "):
            items.append(Entry("", pending, [line]))
        else:
            items[-1].lines.extend(pending)
            items[-1].lines.append(line)
        pending = []
    return indent or 0, items, pending


def _item_value(item: Entry) -> Any:
    text = "".join(item.lines)
    return yaml.safe_load(text)[0]


def _shift(lines: List[str], delta: int) -> List[str]:
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
        elif delta >= 0:
            out.append(" " * delta + line)
        else:
            assert line[:-delta].strip() == "", line
            out.append(line[-delta:])
    return out


def _ensure_newlines(lines: List[str]) -> List[str]:
    return [line if line.endswith("\n") else line + "\n" for line in lines]


def _comment_of(line: str) -> Optional[str]:
    """A line's comment: the whole line's, or a trailing ` # ...` outside
    quotes; None when it has none."""
    stripped = line.strip()
    if stripped.startswith("#"):
        return stripped
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'" and (index == 0 or line[index - 1] in " -:,[{"):
            quote = char
        elif char == "#" and index > 0 and line[index - 1] in " \t":
            return line[index:].strip()
    return None


def _comments(lines: Iterable[str]) -> List[str]:
    """Every comment in some lines, in order."""
    return [comment for comment in map(_comment_of, lines) if comment]


def _comment_lines(
    comments: Iterable[str], indent: int, have: Iterable[str]
) -> List[str]:
    """Comment lines at an indent for the comments not already in `have` (a
    comment written the same in two devices is kept once)."""
    seen = set(have)
    out = []
    for comment in comments:
        if comment not in seen:
            seen.add(comment)
            out.append(" " * indent + comment + "\n")
    return out


def _minus(comments: List[str], *others: Iterable[str]) -> List[str]:
    """`comments` less one occurrence of each comment of `others`, in order."""
    left = Counter(comments)
    for other in others:
        left.subtract(other)
    out = []
    for comment in comments:
        if left[comment] > 0:
            left[comment] -= 1
            out.append(comment)
    return out


# ---- fixes ---------------------------------------------------------------------


class _Step:
    """One `devices:` step's text: the `- ` line's mapping, editable."""

    def __init__(self, lines: List[str]):
        first = lines[0]
        dash = _indent(first)
        assert first[dash:].startswith("- "), first
        self.dash = dash
        self.indent = dash + 2
        lines = _ensure_newlines(lines)
        lines[0] = " " * self.indent + first[dash + 2 :]
        self.block = parse_block(lines, self.indent)

    def lines(self) -> List[str]:
        out: List[str] = []
        for index, entry in enumerate(self.block.entries):
            body = list(entry.lines)
            if index == 0:
                line = body[0]
                body[0] = " " * self.dash + "- " + line[self.indent :]
            out.extend(entry.lead + body)
        return out + self.block.trailing

    def devices(self) -> Block:
        entry = self.block.get(DEVICES_FIELD)
        assert entry is not None
        return child_block(entry, self.indent + 2)

    def set_devices(self, block: Block) -> None:
        set_child_block(self.block.get(DEVICES_FIELD), block)

    def device(self, name: str) -> Tuple[Block, Entry]:
        devices = self.devices()
        entry = devices.get(name)
        return devices, entry


def _device_body(entry: Entry, devices: Block) -> Block:
    return child_block(entry, devices.indent + 2)


def _set_device_body(step: _Step, name: str, body: Block) -> None:
    devices = step.devices()
    entry = devices.get(name)
    header = entry.lines[0]
    if not body.entries and not body.trailing:
        # `h100:` with nothing under it.
        entry.lines = [header]
    else:
        entry.lines = [header] + body.lines()
    step.set_devices(devices)


def _remove_from_list(body: Block, name: str, values: List[Any]) -> None:
    entry = body.get(name)
    if entry is None:
        return
    indent, items, trailing = _items(entry)
    kept = [item for item in items if _item_value(item) not in values]
    if not kept:
        body.entries.remove(entry)
        return
    entry.lines = (
        [entry.lines[0]] + [line for item in kept for line in item.text()] + trailing
    )


def _remove_from_map(body: Block, name: str, keys: List[str]) -> None:
    entry = body.get(name)
    if entry is None:
        return
    child = child_block(entry, body.indent + 2)
    child.entries = [e for e in child.entries if e.key not in keys]
    if not child.entries:
        body.entries.remove(entry)
    else:
        set_child_block(entry, child)


def _hoist(step: _Step, raw: Dict[str, Any], found: List[Tuple[str, Any]]) -> None:
    """Move each found value from the devices to the step level."""
    deps = [value for name, value in found if name == DEPENDENCIES_FIELD]
    env = [value for name, value in found if name == ENV_FIELD]
    whole = [name for name, value in found if value is None]
    moved_items: Dict[str, List[Entry]] = {DEPENDENCIES_FIELD: [], ENV_FIELD: []}
    moved_whole: Dict[str, Entry] = {}
    # The text of each value is moved from the first device that has it; the
    # comments of the other devices' copies go with it: those on an item (a
    # dependency, an env var) above the moved item, the rest (above a field
    # the move empties, say) above the step-level field.
    item_notes: Dict[Tuple[str, Any], List[str]] = {}
    field_notes: Dict[str, List[str]] = {}
    fields = ([DEPENDENCIES_FIELD] if deps else []) + ([ENV_FIELD] if env else [])
    devices = step.devices()
    for device_entry in devices.entries:
        body = _device_body(device_entry, devices)
        delta = step.indent - body.indent
        before = {
            name: body.get(name).text() for name in fields + whole if body.get(name)
        }
        moved_here: Dict[str, List[str]] = {}
        noted_here: Dict[str, List[str]] = {}
        if deps and body.get(DEPENDENCIES_FIELD):
            _, items, _ = _items(body.get(DEPENDENCIES_FIELD))
            shared = [item for item in items if _item_value(item) in deps]
            if not moved_items[DEPENDENCIES_FIELD]:
                moved_items[DEPENDENCIES_FIELD] = [
                    Entry("", _shift(item.lead, delta), _shift(item.lines, delta))
                    for item in shared
                ]
                moved_here[DEPENDENCIES_FIELD] = [
                    line for item in shared for line in item.text()
                ]
            else:
                for item in shared:
                    notes = _comments(item.text())
                    key = (DEPENDENCIES_FIELD, _item_value(item))
                    item_notes.setdefault(key, []).extend(notes)
                    noted_here.setdefault(DEPENDENCIES_FIELD, []).extend(notes)
        if env and body.get(ENV_FIELD):
            child = child_block(body.get(ENV_FIELD), body.indent + 2)
            shared = [
                e
                for e in child.entries
                if (e.key, yaml.safe_load("".join(e.lines))[e.key]) in env
            ]
            if not moved_items[ENV_FIELD]:
                moved_items[ENV_FIELD] = [
                    Entry(e.key, _shift(e.lead, delta), _shift(e.lines, delta))
                    for e in shared
                ]
                moved_here[ENV_FIELD] = [line for e in shared for line in e.text()]
            else:
                for e in shared:
                    notes = _comments(e.text())
                    item_notes.setdefault((ENV_FIELD, e.key), []).extend(notes)
                    noted_here.setdefault(ENV_FIELD, []).extend(notes)
        for name in whole:
            if name not in moved_whole and body.get(name):
                entry = body.get(name)
                moved_whole[name] = Entry(
                    name, _shift(entry.lead, delta), _shift(entry.lines, delta)
                )
                moved_here[name] = before[name]
        _remove_from_list(body, DEPENDENCIES_FIELD, deps)
        _remove_from_map(body, ENV_FIELD, [var for var, _ in env])
        body.entries = [e for e in body.entries if e.key not in whole]
        for name, text in before.items():
            after = body.get(name).text() if body.get(name) else []
            # Less what stays, what moved and what is noted on an item.
            field_notes.setdefault(name, []).extend(
                _minus(
                    _comments(text),
                    _comments(after),
                    _comments(moved_here.get(name, [])),
                    noted_here.get(name, []),
                )
            )
        device_entry.lines = [device_entry.lines[0]] + (
            body.lines() if body.entries or body.trailing else []
        )
    step.set_devices(devices)

    for (name, value), notes in item_notes.items():
        for item in moved_items[name]:
            if (item.key if name == ENV_FIELD else _item_value(item)) == value:
                indent = next(_indent(line) for line in item.lines if line.strip())
                item.lead += _comment_lines(notes, indent, _comments(item.text()))
    for name, entry in moved_whole.items():
        step.block.entries.append(entry)
    _add_items(step, DEPENDENCIES_FIELD, moved_items[DEPENDENCIES_FIELD], is_list=True)
    _add_items(step, ENV_FIELD, moved_items[ENV_FIELD], is_list=False)
    for name, notes in field_notes.items():
        entry = step.block.get(name)
        entry.lead += _comment_lines(notes, step.indent, _comments(entry.text()))


def _add_items(step: _Step, name: str, items: List[Entry], is_list: bool) -> None:
    if not items:
        return
    entry = step.block.get(name)
    if entry is None:
        child_indent = step.indent + 2
        entry = Entry(name, [], [" " * step.indent + f"{name}:\n"])
        step.block.entries.append(entry)
        current = None
    else:
        current = _child_indent(entry)
        child_indent = current
    first_indent = next(
        _indent(line) for item in items for line in item.lines if line.strip()
    )
    delta = child_indent - first_indent
    for item in items:
        entry.lines.extend(_shift(item.lead, delta) + _shift(item.lines, delta))


def _same_as_fix(step: _Step, target: str, device: str) -> None:
    devices = step.devices()
    entry = devices.get(device)
    body = _device_body(entry, devices)
    kept = [e for e in body.entries if e.key in IDENTITY]
    # The comments of the fields `same_as` replaces stay above it: those above
    # a field as written, those inside one as whole lines.
    dropped_comments: List[str] = []
    for e in body.entries:
        if e.key not in IDENTITY:
            dropped_comments += e.lead + _comment_lines(
                _comments(e.lines), body.indent, _comments(dropped_comments)
            )
    line = " " * body.indent + f"{SAME_AS_FIELD}: {target}\n"
    body.entries = [Entry(SAME_AS_FIELD, dropped_comments, [line])] + kept
    entry.lines = [entry.lines[0]] + body.lines()
    step.set_devices(devices)


def _sort_block(block: Block, rank) -> None:
    block.entries = sorted(block.entries, key=lambda entry: rank(entry.key))


def _order_fix(step: _Step, raw: Dict[str, Any]) -> None:
    _sort_block(step.block, lambda name: _rank(STEP_ORDER, name))
    devices = step.devices()
    old_keys = _keys_of(raw, [entry.key for entry in devices.entries])
    _sort_block(devices, _device_rank)
    new_order = [entry.key for entry in devices.entries]
    new_keys = _keys_of(raw, new_order)
    for entry in devices.entries:
        body = _device_body(entry, devices)
        _sort_block(body, lambda name: _rank(DEVICE_ORDER + (VARIANTS_FIELD,), name))
        if old_keys[entry.key] != new_keys[entry.key]:
            # Keep the job's key: pin it.
            indent = body.indent
            line = " " * indent + f"key: {old_keys[entry.key]}\n"
            body.entries.append(Entry("key", [], [line]))
            _sort_block(
                body, lambda name: _rank(DEVICE_ORDER + (VARIANTS_FIELD,), name)
            )
        variants = body.get(VARIANTS_FIELD)
        if variants is not None:
            names = child_block(variants, body.indent + 2)
            for variant in names.entries:
                variant_body = child_block(variant, names.indent + 2)
                _sort_block(variant_body, lambda name: _rank(VARIANT_ORDER, name))
                set_child_block(variant, variant_body)
            set_child_block(variants, names)
        if body.entries or body.trailing:
            set_child_block(entry, body)
    step.set_devices(devices)


def _keys_of(raw: Dict[str, Any], order: List[str]) -> Dict[str, str]:
    entries = _entries(raw)
    return default_keys(
        raw["key"], [(name, entries[name].get("key")) for name in order]
    )


def fix_step_lines(
    lines: List[str], group_depends_on: Any = None
) -> Tuple[List[str], List[str]]:
    """The fixed text of one `devices:` step, and why a fix was refused."""
    raw = yaml.safe_load("".join(lines))[0]
    before = compile_device_step(raw, group_depends_on)
    step = _Step(lines)
    for target, devices in _same_as_candidates(_entries(raw)):
        for device in devices:
            _same_as_fix(step, target, device)
    raw = yaml.safe_load("".join(step.lines()))[0]
    found = _hoistable(raw)
    if found:
        _hoist(step, raw, found)
        raw = yaml.safe_load("".join(step.lines()))[0]
        if _hoistable(raw):
            return lines, ["could not hoist every shared value; fix it by hand"]
    _order_fix(step, raw)
    fixed = step.lines()
    raw = yaml.safe_load("".join(fixed))[0]
    try:
        after = compile_device_step(raw, group_depends_on)
    except DeviceStepError as error:
        return lines, [f"the fix would not compile ({error}); fix it by hand"]
    if _normalized(after) != _normalized(before):
        return lines, ["the fix would change a job; fix it by hand"]
    lost = set(_comments(lines)) - set(_comments(fixed))
    if lost:
        return lines, [
            f"the fix would drop the comment {sorted(lost)[0]!r}; fix it by hand"
        ]
    return fixed, []


def _normalized(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compiled steps with their order and dependency order ignored (neither
    changes a job)."""
    out = sorted(copy.deepcopy(steps), key=lambda step: step["key"])
    for step in out:
        for block in (step, (step.get("mirror") or {}).get(MIRROR_HARDWARE) or {}):
            if DEPENDENCIES_FIELD in block:
                block[DEPENDENCIES_FIELD] = sorted(block[DEPENDENCIES_FIELD])
    return out


def fix_text(text: str, path: str = "<yaml>") -> Tuple[str, List[Finding]]:
    """The file with L1, L2 and L4 fixed in every valid `devices:` step, and
    what was refused."""
    doc = yaml.safe_load(text)
    steps = doc.get("steps") if isinstance(doc, dict) else None
    if not isinstance(steps, list):
        return text, []
    starts = _step_lines(text)
    lines = text.splitlines(keepends=True)
    refused: List[Finding] = []
    flow = {line for line, _ in flow_style_nodes(text)}
    # From the last step up, so earlier line numbers stay put.
    for index in reversed(range(len(steps))):
        raw = steps[index]
        if not is_device_step(raw):
            continue
        try:
            resolve_step(raw, doc.get("depends_on"))
        except DeviceStepError:
            continue
        if not any(code != "L3" for code, _ in step_findings(raw)):
            continue
        start = starts[index] - 1
        end = _step_end(lines, starts, index)
        if any(start < line <= end for line in flow):
            continue
        region = lines[start:end]
        fixed, why = fix_step_lines(region, doc.get("depends_on"))
        if why:
            refused.extend(Finding(path, start + 1, "L0", reason) for reason in why)
            continue
        lines[start:end] = fixed
    return "".join(lines), refused


def _step_end(lines: List[str], starts: List[int], index: int) -> int:
    """The end (exclusive, 0-based) of a step's lines: up to the next step's
    `- ` line or the end of the steps list, less trailing comments and blank
    lines (they open what follows)."""
    start = starts[index] - 1
    dash = _indent(lines[start])
    if index + 1 < len(starts):
        end = starts[index + 1] - 1
    else:
        end = start + 1
        while end < len(lines) and (
            _is_comment_or_blank(lines[end]) or _indent(lines[end]) > dash
        ):
            end += 1
    while end > start + 1 and _is_comment_or_blank(lines[end - 1]):
        end -= 1
    return end


# ---- files ---------------------------------------------------------------------


def yaml_files(paths: Iterable[str]) -> List[str]:
    files = []
    for path in paths:
        if os.path.isdir(path):
            for root, _, names in os.walk(path):
                files.extend(
                    os.path.join(root, name)
                    for name in sorted(names)
                    if name.endswith(".yaml")
                )
        else:
            files.append(path)
    return sorted(files)


def lint_paths(paths: Iterable[str], fix: bool = False) -> List[Finding]:
    texts: Dict[str, str] = {}
    findings: List[Finding] = []
    for path in yaml_files(paths):
        with open(path) as f:
            text = f.read()
        if fix:
            fixed, refused = fix_text(text, path)
            findings.extend(refused)
            if fixed != text:
                with open(path, "w") as f:
                    f.write(fixed)
                text = fixed
        texts[path] = text
        findings.extend(lint_text(text, path))
    findings.extend(lint_keys(texts))
    return findings
