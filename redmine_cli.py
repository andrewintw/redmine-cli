#!/usr/bin/env python3
import argparse
import csv
import getpass
import io
import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

PLACEHOLDER = "在這裡填你的金鑰"
TRUE_VALUES = {"1", "true", "yes", "on"}
COMMANDS = {"export", "projects", "issues", "issue", "stats", "assignees", "meta"}


class IssueNotFound(SystemExit):
    pass


ISSUE_INCLUDE_CHOICES = (
    "journals",
    "attachments",
    "relations",
    "children",
    "changesets",
    "watchers",
    "allowed_statuses",
)
RELATION_TYPE_INVERSE = {
    "blocks": "blocked",
    "blocked": "blocks",
    "precedes": "follows",
    "follows": "precedes",
    "copied_to": "copied_from",
    "copied_from": "copied_to",
    "duplicates": "duplicated",
    "duplicated": "duplicates",
}
EXPORT_ONLY_FLAGS = {
    "--dry-run",
    "--output-dir",
    "--page-size",
    "--csv-description-limit",
}
CSV_COLUMNS = [
    "id",
    "project_id",
    "project_name",
    "tracker",
    "status",
    "priority",
    "subject",
    "description",
    "description_truncated",
    "author",
    "assigned_to",
    "category",
    "fixed_version",
    "parent_id",
    "start_date",
    "due_date",
    "created_on",
    "updated_on",
    "closed_on",
    "estimated_hours",
    "spent_hours",
    "done_ratio",
    "is_private",
    "custom_fields",
]


def load_env_file(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def check_permissions(path):
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        print(f"警告: {path} 權限為 {oct(mode)}，建議執行 chmod 600 {path}", file=sys.stderr)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def read_key_file(path):
    key = path.read_text(encoding="utf-8").strip()
    if not key or key == PLACEHOLDER:
        raise SystemExit(f"錯誤: 請先將金鑰填入 {path}")
    return key


def prompt_api_key():
    interactive = sys.stdin.isatty()
    if not interactive:
        try:
            descriptor = os.open("/dev/tty", os.O_RDWR)
            interactive = os.isatty(descriptor)
            os.close(descriptor)
        except OSError:
            interactive = False
    if not interactive:
        raise SystemExit(
            "錯誤: 目前環境無法互動輸入 API 金鑰。\n"
            "請改用下列方式之一：\n"
            "  1. 建立 .env（可參考 .env.example）\n"
            "  2. 設定環境變數 REDMINE_API_KEY\n"
            "  3. 使用 --api-key-file /path/to/key（或 /dev/stdin）"
        )
    try:
        key = getpass.getpass("Redmine API key: ").strip()
    except EOFError:
        raise SystemExit("錯誤: 未提供 API 金鑰")
    except OSError:
        print("警告: 無法隱藏輸入，金鑰會顯示在畫面上。", file=sys.stderr)
        try:
            key = input("Redmine API key: ").strip()
        except EOFError:
            raise SystemExit("錯誤: 未提供 API 金鑰")
    if not key:
        raise SystemExit("錯誤: 未提供 API 金鑰")
    return key


def resolve_api_key(args):
    if args.api_key_file:
        path = Path(args.api_key_file).expanduser()
        if not path.exists():
            raise SystemExit(f"錯誤: 金鑰檔不存在: {path}")
        check_permissions(path)
        return read_key_file(path)
    key = os.environ.get("REDMINE_API_KEY", "").strip()
    if key:
        if key == PLACEHOLDER:
            raise SystemExit("錯誤: 請先編輯 .env，將 REDMINE_API_KEY 換成你的金鑰")
        return key
    return prompt_api_key()


def ssl_error_message(error):
    return (
        "錯誤: TLS 憑證驗證失敗，已停止重試。\n"
        "  1. 提供 CA bundle：--ca-cert /path/to/ca.pem\n"
        "     或設定 REQUESTS_CA_BUNDLE=/path/to/ca.pem\n"
        "  2. 僅限受信任內網：加上 --insecure，或設定 REDMINE_INSECURE=true\n"
        f"詳細: {error}"
    )


class RedmineClient:
    def __init__(self, base_url, api_key, insecure, timeout, max_retries, ca_cert=None):
        base_url = base_url.strip()
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.verify = ca_cert if ca_cert else not insecure
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Redmine-API-Key": api_key,
                "Accept": "application/json",
                "User-Agent": "redmine-cli/1.0",
            }
        )
        if insecure:
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )

    def get_json(self, path, params=None, not_found=None):
        url = self.base_url + path
        delay = 2.0
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                    verify=self.verify,
                )
            except requests.exceptions.SSLError as exc:
                raise SystemExit(ssl_error_message(exc))
            except requests.RequestException as exc:
                message = str(exc)
                if "CERTIFICATE_VERIFY_FAILED" in message or "certificate verify failed" in message:
                    raise SystemExit(ssl_error_message(exc))
                last_error = message
                if attempt == self.max_retries:
                    break
                print(
                    f"警告: 連線失敗 ({exc})，{delay:.0f} 秒後重試...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if response.status_code == 401:
                raise SystemExit("錯誤: 401 未授權，請確認 REDMINE_API_KEY 正確且未過期")
            if response.status_code == 403:
                raise SystemExit("錯誤: 403 權限不足，該帳號無法讀取此範圍的資料")
            if response.status_code == 404:
                if not_found:
                    raise IssueNotFound(not_found)
                raise SystemExit("錯誤: 404 找不到 API，請確認網址與 REST API 是否啟用")
            if response.status_code == 429 or 500 <= response.status_code < 600:
                last_error = f"HTTP {response.status_code}"
                if attempt == self.max_retries:
                    break
                retry_after = response.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else delay
                print(
                    f"警告: 伺服器回應 {response.status_code}，{wait:.0f} 秒後重試...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                delay = min(delay * 2, 30)
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise SystemExit(f"錯誤: HTTP {response.status_code}: {exc}")

            try:
                return response.json()
            except ValueError:
                raise SystemExit("錯誤: 伺服器回應不是 JSON，請確認 API 網址正確")

        raise SystemExit(f"錯誤: 重試 {self.max_retries} 次後仍失敗: {last_error}")


def fetch_all(client, path, key, params=None, page_size=100, progress=False, max_results=0):
    results = []
    offset = 0
    total = None
    while True:
        limit = page_size
        if max_results:
            remaining = max_results - len(results)
            if remaining <= 0:
                break
            limit = min(limit, remaining)
        query = dict(params or {})
        query["limit"] = limit
        query["offset"] = offset
        data = client.get_json(path, query)
        batch = data.get(key) or []
        if total is None:
            total = data.get("total_count")
            if progress and total is not None:
                print(f"共 {total} 筆，開始下載...", file=sys.stderr)
        results.extend(batch)
        if progress:
            shown = total if total is not None else "?"
            print(
                f"\r已取得 {len(results)}/{shown}",
                end="",
                file=sys.stderr,
                flush=True,
            )
        if not batch:
            break
        offset += len(batch)
        if total is not None and offset >= total:
            break
        if total is None and len(batch) < limit:
            break
        if max_results and len(results) >= max_results:
            break
    if progress:
        print(file=sys.stderr)
    return results


def fetch_issues(client, project, page_size):
    params = {"status_id": "*", "sort": "id:asc"}
    if project:
        params["project_id"] = project
    return fetch_all(
        client,
        "/issues.json",
        "issues",
        params=params,
        page_size=page_size,
        progress=True,
    )


def dry_run(client, project, project_label=None):
    params = {"status_id": "*", "limit": 1, "offset": 0, "sort": "id:asc"}
    if project:
        params["project_id"] = project
    data = client.get_json("/issues.json", params)
    total = data.get("total_count", len(data.get("issues") or []))
    scope = f"專案 {project_label or project}" if project else "全部專案"
    print(f"{scope} 共 {total} 筆 issue（所有狀態）")


def value_name(value):
    if isinstance(value, dict):
        if value.get("name") is not None:
            return str(value["name"]).strip()
        return value.get("id", "")
    if value is None:
        return ""
    return str(value).strip()


def flatten_issue(issue, description_limit=0):
    description = issue.get("description") or ""
    truncated = False
    if description_limit and len(description) > description_limit:
        description = description[:description_limit] + "\n...[truncated]"
        truncated = True
    custom_fields = issue.get("custom_fields") or []
    custom_text = "; ".join(
        f"{field.get('name', '')}={field.get('value', '')}"
        for field in custom_fields
        if field.get("value") not in (None, "")
    )
    project = issue.get("project") or {}
    parent = issue.get("parent") or {}
    return {
        "id": issue.get("id", ""),
        "project_id": project.get("id", ""),
        "project_name": project.get("name", ""),
        "tracker": value_name(issue.get("tracker")),
        "status": value_name(issue.get("status")),
        "priority": value_name(issue.get("priority")),
        "subject": issue.get("subject", ""),
        "description": description,
        "description_truncated": truncated,
        "author": value_name(issue.get("author")),
        "assigned_to": value_name(issue.get("assigned_to")),
        "category": value_name(issue.get("category")),
        "fixed_version": value_name(issue.get("fixed_version")),
        "parent_id": parent.get("id", ""),
        "start_date": issue.get("start_date", ""),
        "due_date": issue.get("due_date", ""),
        "created_on": issue.get("created_on", ""),
        "updated_on": issue.get("updated_on", ""),
        "closed_on": issue.get("closed_on", ""),
        "estimated_hours": issue.get("estimated_hours", ""),
        "spent_hours": issue.get("spent_hours", ""),
        "done_ratio": issue.get("done_ratio", ""),
        "is_private": issue.get("is_private", ""),
        "custom_fields": custom_text,
    }


def write_json(path, issues):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(issues, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_csv(path, issues, description_limit=0):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for issue in issues:
            writer.writerow(flatten_issue(issue, description_limit))


def display_width(text):
    width = 0
    for char in str(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate_display(text, width):
    text = str(text)
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    ellipsis = "…"
    target = width - display_width(ellipsis)
    chars = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        )
        if used + char_width > target:
            break
        chars.append(char)
        used += char_width
    return "".join(chars) + ellipsis


def pad_display(text, width):
    return str(text) + " " * max(0, width - display_width(text))


def align_display(text, width, alignment="left"):
    if alignment == "right":
        return " " * max(0, width - display_width(text)) + str(text)
    return pad_display(text, width)


def render_table(
    headers,
    rows,
    max_widths=None,
    min_widths=None,
    terminal_width=None,
    alignments=None,
):
    headers = [str(header) for header in headers]
    rows = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    column_count = len(headers)
    widths = [display_width(header) for header in headers]
    for row in rows:
        for index in range(column_count):
            widths[index] = max(widths[index], display_width(row[index]))
    if max_widths:
        for index in range(min(len(max_widths), column_count)):
            limit = max_widths[index]
            if limit:
                widths[index] = min(widths[index], limit)
    if min_widths:
        for index in range(min(len(min_widths), column_count)):
            limit = min_widths[index]
            if limit:
                widths[index] = max(widths[index], limit)
    terminal_width = terminal_width or shutil.get_terminal_size((120, 24)).columns
    separator = 2
    minimums = [4] * column_count
    if min_widths:
        for index in range(min(len(min_widths), column_count)):
            minimums[index] = min_widths[index]
    while sum(widths) + separator * (column_count - 1) > terminal_width:
        candidates = [
            (widths[index] - minimums[index], index)
            for index in range(column_count)
        ]
        excess, index = max(candidates)
        if excess <= 0:
            break
        widths[index] -= 1
    def cell(text, index):
        alignment = (
            alignments[index]
            if alignments and index < len(alignments)
            else "left"
        )
        return align_display(
            truncate_display(text, widths[index]), widths[index], alignment
        )

    lines = []
    lines.append(
        "  ".join(cell(headers[index], index) for index in range(column_count)).rstrip()
    )
    lines.append(
        "  ".join("-" * widths[index] for index in range(column_count)).rstrip()
    )
    for row in rows:
        lines.append(
            "  ".join(cell(row[index], index) for index in range(column_count)).rstrip()
        )
    return "\n".join(lines) + "\n"


def csv_text(headers, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue()


def emit_text(text, path=None, bom=False):
    if path:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        encoding = "utf-8-sig" if bom else "utf-8"
        path.write_text(text, encoding=encoding)
        print(f"已寫入 {path}", file=sys.stderr)
        return
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")


def resolve_ids(items, raw, kind):
    tokens = [token.strip() for token in str(raw).split(",") if token.strip()]
    if not tokens:
        raise SystemExit(f"錯誤: 未指定 {kind}")
    resolved = []
    for token in tokens:
        if token.isdigit():
            match = next((item for item in items if str(item.get("id")) == token), None)
            if not match:
                names = ", ".join(str(item.get("name")) for item in items[:10])
                raise SystemExit(f"錯誤: 找不到 {kind} id={token}。可用選項: {names}")
            resolved.append(token)
            continue
        exact = [
            item
            for item in items
            if str(item.get("name", "")).strip().casefold() == token.casefold()
        ]
        matches = exact or [
            item
            for item in items
            if token.casefold() in str(item.get("name", "")).casefold()
        ]
        if len(matches) == 1:
            resolved.append(str(matches[0]["id"]))
        elif not matches:
            names = ", ".join(str(item.get("name")) for item in items[:10])
            raise SystemExit(f"錯誤: 找不到 {kind} '{token}'。可用選項: {names}")
        else:
            names = ", ".join(str(item.get("name")) for item in matches)
            raise SystemExit(f"錯誤: {kind} '{token}' 不明確，候選: {names}")
    return list(dict.fromkeys(resolved))


def resolve_status_values(raw, statuses):
    value = (raw or "open").strip()
    lowered = value.lower()
    if lowered in ("all", "*"):
        return ["*"]
    if lowered in ("open", "closed"):
        return [lowered]
    return resolve_ids(statuses, value, "Status")


def find_project(projects, raw):
    value = str(raw).strip()
    lowered = value.casefold()
    for project in projects:
        if str(project.get("id")) == value:
            return project
        if str(project.get("identifier", "")).casefold() == lowered:
            return project
        if str(project.get("name", "")).strip().casefold() == lowered:
            return project
    names = ", ".join(str(project.get("name")) for project in projects[:10])
    raise SystemExit(f"錯誤: 找不到專案 '{raw}'。可用選項: {names}")


def resolve_project(raw, projects):
    project = find_project(projects, raw)
    return project.get("identifier") or str(project["id"])


def iterate_project_nodes(nodes):
    for project in nodes:
        yield project
        yield from iterate_project_nodes(project.get("_children", []))


def iterate_project_rows(nodes, depth=0):
    for project in nodes:
        yield project, depth
        yield from iterate_project_rows(project.get("_children", []), depth + 1)


def subtree_count(project_id, children, direct, memo):
    if project_id in memo:
        return memo[project_id]
    total = direct.get(project_id, 0)
    for child in children.get(project_id, []):
        total += subtree_count(child["id"], children, direct, memo)
    memo[project_id] = total
    return total


def filter_tree_nodes(roots, children, subtree, hide_empty):
    result = []
    for project in roots:
        if hide_empty and subtree.get(project["id"], 0) == 0:
            continue
        item = dict(project)
        item["_children"] = filter_tree_nodes(
            children.get(project["id"], []), children, subtree, hide_empty
        )
        result.append(item)
    return result


def project_count_suffix(direct_count, subtree_total, counting):
    if not counting:
        return ""
    if subtree_total == direct_count:
        return f"  open {direct_count}"
    return f"  open {direct_count}（含子專案 {subtree_total}）"


def render_project_tree(nodes, direct, subtree, counting, prefix=""):
    lines = []
    for index, project in enumerate(nodes):
        last = index == len(nodes) - 1
        connector = "└─ " if last else "├─ "
        label = f"[{str(project.get('name', '')).strip()}]"
        label += project_count_suffix(
            direct.get(project["id"], 0), subtree.get(project["id"], 0), counting
        )
        lines.append(prefix + connector + label)
        child_prefix = prefix + ("   " if last else "│  ")
        lines.extend(
            render_project_tree(
                project.get("_children", []),
                direct,
                subtree,
                counting,
                child_prefix,
            )
        )
    return lines


def render_project_roots(nodes, direct, subtree, counting):
    lines = []
    previous_had_children = False
    for index, project in enumerate(nodes):
        has_children = bool(project.get("_children"))
        if index and (has_children or previous_had_children):
            lines.append("")
        label = f"[{str(project.get('name', '')).strip()}]"
        label += project_count_suffix(
            direct.get(project["id"], 0), subtree.get(project["id"], 0), counting
        )
        lines.append(label)
        for line in render_project_tree(
            project.get("_children", []), direct, subtree, counting
        ):
            lines.append(line)
        previous_had_children = has_children
    return "\n".join(lines) + ("\n" if lines else "")


def project_table_rows(nodes, direct, subtree, counting):
    rows = []
    for project, depth in iterate_project_rows(nodes):
        name = "  " * depth + str(project.get("name", "")).strip()
        if counting:
            rows.append(
                [
                    name,
                    direct.get(project["id"], 0),
                    subtree.get(project["id"], 0),
                ]
            )
        else:
            rows.append([name])
    return rows


def project_summary_rows(roots, direct, subtree):
    ordered = sorted(
        roots,
        key=lambda project: (
            -subtree.get(project["id"], 0),
            str(project.get("name", "")).casefold(),
        ),
    )
    return [
        [
            str(project.get("name", "")).strip(),
            direct.get(project["id"], 0),
            subtree.get(project["id"], 0),
        ]
        for project in ordered
    ]


def wrap_tokens(tokens, width):
    lines = []
    current = ""
    for token in tokens:
        if not current:
            current = token
        elif display_width(current) + 1 + display_width(token) <= width:
            current += " " + token
        else:
            lines.append(current)
            current = token
    if current:
        lines.append(current)
    return "\n".join(lines) + ("\n" if lines else "")


def build_project_tree(projects):
    by_id = {project["id"]: project for project in projects}
    children = defaultdict(list)
    roots = []
    for project in projects:
        parent = (project.get("parent") or {}).get("id")
        if parent in by_id:
            children[parent].append(project)
        else:
            roots.append(project)
    for bucket in children.values():
        bucket.sort(key=lambda item: str(item.get("name", "")).casefold())
    roots.sort(key=lambda item: str(item.get("name", "")).casefold())
    return roots, children


def get_projects(client):
    return fetch_all(client, "/projects.json", "projects", page_size=100)


def get_trackers(client):
    return client.get_json("/trackers.json").get("trackers") or []


def get_statuses(client):
    return client.get_json("/issue_statuses.json").get("issue_statuses") or []


def get_current_user(client):
    return client.get_json("/users/current.json").get("user") or {}


def normalize_account_name(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = re.sub(r"[\s\-.]+", "_", text)
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    return text.strip("_")


def collect_people(issues, closed_status_ids):
    totals = Counter()
    open_counts = Counter()
    names = {}
    logins = {}
    for issue in issues:
        assignee = issue.get("assigned_to")
        if not assignee:
            continue
        user_id = assignee.get("id")
        if user_id is None:
            continue
        names[user_id] = str(assignee.get("name", "")).strip() or f"User {user_id}"
        login = str(assignee.get("login") or "").strip()
        if login:
            logins[user_id] = login
        totals[user_id] += 1
        status = issue.get("status") or {}
        if status.get("id") not in closed_status_ids:
            open_counts[user_id] += 1
    return [
        {
            "id": user_id,
            "name": names[user_id],
            "login": logins.get(user_id, ""),
            "account": normalize_account_name(names[user_id]),
            "total": totals[user_id],
            "open": open_counts[user_id],
        }
        for user_id in totals
    ]


def fetch_project_people(client, project, page_size=100):
    memberships = fetch_all(
        client,
        f"/projects/{project}/memberships.json",
        "memberships",
        page_size=page_size,
    )
    people = []
    for membership in memberships:
        user = membership.get("user")
        if not user:
            continue
        user_id = user.get("id")
        name = str(user.get("name", "")).strip()
        if user_id is None or not name:
            continue
        people.append(
            {
                "id": user_id,
                "name": name,
                "login": str(user.get("login") or "").strip(),
                "account": normalize_account_name(name),
                "total": 0,
                "open": 0,
            }
        )
    return people


def load_export_issues(output_dir):
    path = Path(output_dir) / "issues.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"錯誤: 無法讀取 {path}: {exc}")
    if not isinstance(data, list):
        raise SystemExit(f"錯誤: {path} 格式不是 issue 清單")
    return data


def assignee_cache_path(output_dir):
    return Path(output_dir) / "assignees.json"


def load_assignee_cache(output_dir, base_url):
    path = assignee_cache_path(output_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("base_url") != base_url:
        return None
    people = data.get("people")
    if not isinstance(people, list):
        return None
    result = []
    for person in people:
        if not isinstance(person, dict):
            continue
        user_id = person.get("id")
        name = str(person.get("name", "")).strip()
        if user_id is None or not name:
            continue
        total = person.get("total", 0)
        open_count = person.get("open", 0)
        login = str(person.get("login") or "").strip()
        account = str(person.get("account") or "").strip() or normalize_account_name(name)
        result.append(
            {
                "id": user_id,
                "name": name,
                "login": login,
                "account": account,
                "total": total if isinstance(total, int) else 0,
                "open": open_count if isinstance(open_count, int) else 0,
            }
        )
    return result or None


def write_assignee_cache(output_dir, base_url, people):
    if not people:
        return
    path = assignee_cache_path(output_dir)
    unique = {}
    for person in people:
        user_id = person.get("id")
        name = str(person.get("name", "")).strip()
        if user_id is None or not name:
            continue
        total = person.get("total", 0)
        open_count = person.get("open", 0)
        login = str(person.get("login") or "").strip()
        account = str(person.get("account") or "").strip() or normalize_account_name(name)
        unique[str(user_id)] = {
            "id": user_id,
            "name": name,
            "login": login,
            "account": account,
            "total": total if isinstance(total, int) else 0,
            "open": open_count if isinstance(open_count, int) else 0,
        }
    payload = {
        "base_url": base_url,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "people": sorted(unique.values(), key=lambda item: item["name"].casefold()),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已更新 assignee 快取: {path}", file=sys.stderr)
    except OSError as exc:
        print(f"警告: 無法寫入 assignee 快取 {path}: {exc}", file=sys.stderr)


def assignee_account(person):
    return (
        str(person.get("login") or "").strip()
        or str(person.get("account") or "").strip()
        or normalize_account_name(person.get("name"))
    )


def account_aliases(value):
    value = str(value or "").strip().casefold()
    if not value:
        return set()
    return {
        value,
        value.replace("_", ""),
        value.replace("_", "."),
        value.replace("_", "-"),
    }


def assignee_aliases(person):
    aliases = set()
    name = str(person.get("name", "")).strip()
    if name:
        aliases.add(name.casefold())
    for key in ("login", "account"):
        value = person.get(key)
        aliases.update(account_aliases(value))
        aliases.update(account_aliases(normalize_account_name(value)))
    aliases.update(account_aliases(normalize_account_name(name)))
    return aliases


def assignee_search_values(person):
    values = [str(person.get("name", "")).strip()]
    values.extend(str(person.get(key) or "").strip() for key in ("login", "account"))
    values.append(normalize_account_name(person.get("name")))
    return [value.casefold() for value in values if value]


def assignee_label(person):
    account = assignee_account(person)
    if account:
        return f"{person['name']}({account}, {person['id']})"
    return f"{person['name']}({person['id']})"


def assignee_candidates(people, limit=15):
    return ", ".join(
        assignee_label(person)
        for person in sorted(people, key=lambda item: item["name"].casefold())[:limit]
    )


def match_assignee(people, value):
    target = value.strip().casefold()
    exact = [person for person in people if target in assignee_aliases(person)]
    matches = exact or [
        person
        for person in people
        if any(target in alias for alias in assignee_search_values(person))
    ]
    if len(matches) == 1:
        return str(matches[0]["id"])
    if len(matches) > 1:
        names = ", ".join(assignee_label(person) for person in matches)
        raise SystemExit(f"錯誤: assignee '{value}' 不明確，候選: {names}")
    return None


def current_user_assignee_id(client, value):
    try:
        user = get_current_user(client)
    except SystemExit:
        return None
    user_id = user.get("id")
    if user_id is None:
        return None
    name = " ".join(
        part
        for part in (
            str(user.get("firstname") or "").strip(),
            str(user.get("lastname") or "").strip(),
        )
        if part
    )
    login = user.get("login")
    aliases = account_aliases(login)
    aliases.update(account_aliases(normalize_account_name(login)))
    if name:
        aliases.add(name.casefold())
        aliases.update(account_aliases(normalize_account_name(name)))
    if value.casefold() in aliases:
        return str(user_id)
    return None


def lookup_user_id_by_login(client, login):
    try:
        data = client.get_json("/users.json", {"name": login, "status": "active"})
    except SystemExit as exc:
        message = str(exc)
        if "403" in message or "404" in message:
            return None
        raise
    users = data.get("users") or []
    exact = [
        user
        for user in users
        if str(user.get("login") or "").casefold() == login.casefold()
    ]
    if len(exact) == 1:
        return str(exact[0]["id"])
    return None


def resolve_assignee(raw, client, output_dir, project=None):
    value = (raw or "me").strip()
    if value.lower() == "me" or value.isdigit():
        return value

    people = None
    cached = load_assignee_cache(output_dir, client.base_url)
    if cached:
        matched = match_assignee(cached, value)
        if matched:
            return matched
        people = cached

    issues = load_export_issues(output_dir)
    if issues is not None:
        exported_people = collect_people(issues, set())
        write_assignee_cache(output_dir, client.base_url, exported_people)
        matched = match_assignee(exported_people, value)
        if matched:
            return matched
        people = exported_people

    matched = current_user_assignee_id(client, value)
    if matched:
        return matched

    if project:
        project_people = fetch_project_people(client, project)
        matched = match_assignee(project_people, value)
        if matched:
            return matched
        if project_people:
            people = project_people

    matched = lookup_user_id_by_login(client, value)
    if matched:
        return matched

    if people:
        raise SystemExit(
            f"錯誤: 找不到 assignee '{value}'。候選: "
            f"{assignee_candidates(people)}\n"
            "提示: 帳號可用顯示名稱或小寫底線形式（例如 Jane Doe -> jane_doe）；"
            "名單可用 assignees --live 重新整理。"
        )

    raise SystemExit(
        f"錯誤: 找不到 assignee 名單，無法解析 '{value}'。\n"
        "請改用數字 ID，或先執行下列其中一個指令：\n"
        "  ./redmine_cli.py assignees --live\n"
        "  ./redmine_cli.py export"
    )


def issue_sort_key(issue, field):
    if field == "id":
        return int(issue.get("id") or 0)
    if field == "project":
        return str((issue.get("project") or {}).get("name", "")).casefold()
    if field in ("tracker", "status", "priority"):
        return value_name(issue.get(field)).casefold()
    if field == "subject":
        return str(issue.get("subject", "")).casefold()
    return str(issue.get(field) or "")


def sort_issues(issues, sort_spec):
    field, separator, direction = sort_spec.partition(":")
    field = field.strip() or "updated_on"
    direction = (direction if separator else "asc").strip().lower()
    if direction not in ("asc", "desc"):
        raise SystemExit(f"錯誤: 無效的排序方向 '{direction}'，請用 asc 或 desc")
    allowed = {
        "id",
        "project",
        "tracker",
        "status",
        "priority",
        "subject",
        "created_on",
        "updated_on",
        "closed_on",
    }
    if field not in allowed:
        raise SystemExit(
            f"錯誤: 無效的排序欄位 '{field}'，可用: {', '.join(sorted(allowed))}"
        )
    issues.sort(key=lambda issue: issue_sort_key(issue, field), reverse=direction == "desc")


def issue_url(client, issue):
    return f"{client.base_url}/issues/{issue.get('id')}"


def issue_record(client, issue):
    project = issue.get("project") or {}
    tracker = issue.get("tracker") or {}
    status = issue.get("status") or {}
    return {
        "id": issue.get("id"),
        "project": value_name(issue.get("project")),
        "project_id": project.get("id"),
        "tracker": value_name(issue.get("tracker")),
        "tracker_id": tracker.get("id"),
        "status": value_name(issue.get("status")),
        "status_id": status.get("id"),
        "priority": value_name(issue.get("priority")),
        "subject": issue.get("subject", ""),
        "author": value_name(issue.get("author")),
        "assigned_to": value_name(issue.get("assigned_to")),
        "done_ratio": issue.get("done_ratio") or 0,
        "created_on": issue.get("created_on", ""),
        "updated_on": issue.get("updated_on", ""),
        "url": issue_url(client, issue),
    }


def command_projects(args, client):
    if args.summary and args.no_count:
        raise SystemExit("錯誤: --summary 需要計數，不能與 --no-count 併用")
    projects = get_projects(client)
    counting = not args.no_count
    if args.hide_empty and not counting:
        print("警告: --hide-empty 需要計數，已忽略此選項", file=sys.stderr)
        args.hide_empty = False
    direct = Counter()
    if counting:
        print("統計未關閉 issue...", file=sys.stderr)
        open_issues = fetch_all(
            client,
            "/issues.json",
            "issues",
            params={"status_id": "open", "sort": "id:asc"},
            page_size=100,
        )
        for issue in open_issues:
            project = issue.get("project") or {}
            if project.get("id") is not None:
                direct[project["id"]] += 1

    roots, children = build_project_tree(projects)
    memo = {}
    subtree = {
        project["id"]: subtree_count(project["id"], children, direct, memo)
        for project in projects
    }
    if args.project:
        roots = [find_project(projects, args.project)]
    roots = filter_tree_nodes(roots, children, subtree, args.hide_empty)
    flat_projects = list(iterate_project_nodes(roots))

    if args.summary:
        rows = project_summary_rows(roots, direct, subtree)
        emit_text(
            render_table(
                ["Group", "Open", "Subtree"],
                rows,
                max_widths=[52, 6, 8],
                min_widths=[20, 4, 4],
                alignments=["left", "right", "right"],
            ),
            args.output,
        )
        return

    mode = "flat" if args.flat else args.format
    if mode == "table":
        rows = project_table_rows(roots, direct, subtree, counting)
        if counting:
            headers = ["Project", "Open", "Subtree"]
            max_widths = [60, 6, 8]
            min_widths = [20, 4, 4]
        else:
            headers = ["Project"]
            max_widths = [60]
            min_widths = [20]
        emit_text(
            render_table(
                headers,
                rows,
                max_widths=max_widths,
                min_widths=min_widths,
                alignments=["left", "right", "right"] if counting else ["left"],
            ),
            args.output,
        )
        return

    if mode == "flat":
        width = shutil.get_terminal_size((120, 24)).columns
        tokens = [f"[{str(project.get('name', '')).strip()}]" for project in flat_projects]
        emit_text(wrap_tokens(tokens, width), args.output)
        return

    if mode == "tree":
        emit_text(
            render_project_roots(roots, direct, subtree, counting),
            args.output,
        )
        return

    records = []
    for project in flat_projects:
        parent = project.get("parent") or {}
        records.append(
            {
                "id": project.get("id"),
                "name": str(project.get("name", "")).strip(),
                "identifier": project.get("identifier", ""),
                "parent_id": parent.get("id", ""),
                "parent_name": str(parent.get("name", "")).strip(),
                "open": direct.get(project["id"], 0) if counting else None,
                "open_subtree": subtree.get(project["id"], 0) if counting else None,
            }
        )

    if args.format == "json":
        emit_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            args.output,
        )
        return
    if args.format == "csv":
        headers = [
            "id",
            "name",
            "identifier",
            "parent_id",
            "parent_name",
            "open",
            "open_subtree",
        ]
        rows = [
            [
                record["id"],
                record["name"],
                record["identifier"],
                record["parent_id"],
                record["parent_name"],
                "" if record["open"] is None else record["open"],
                "" if record["open_subtree"] is None else record["open_subtree"],
            ]
            for record in records
        ]
        emit_text(csv_text(headers, rows), args.output, bom=True)
        return

    headers = ["ID", "Project", "Identifier", "Parent"]
    rows = [
        [
            record["id"],
            record["name"],
            record["identifier"],
            record["parent_name"],
        ]
        for record in records
    ]
    if counting:
        headers.extend(["Open", "Subtree"])
        for row, record in zip(rows, records):
            row.extend([record["open"], record["open_subtree"]])
    emit_text(
        render_table(
            headers,
            rows,
            max_widths=[6, 34, 38, 24, 6, 8],
            min_widths=[3, 12, 10, 8, 3, 4],
            alignments=["right", "left", "left", "left", "right", "right"],
        ),
        args.output,
    )


def command_issues(args, client, output_dir, page_size):
    statuses = get_statuses(client)
    trackers = get_trackers(client)
    status_values = resolve_status_values(args.status, statuses)
    tracker_values = (
        resolve_ids(trackers, args.tracker, "Tracker") if args.tracker else [None]
    )
    project = None
    if args.project:
        project = resolve_project(args.project, get_projects(client))
    assignee = resolve_assignee(args.assignee, client, output_dir, project)

    limit = max(0, args.limit)
    merged = {}
    for status_value in status_values:
        for tracker_value in tracker_values:
            params = {
                "status_id": status_value,
                "assigned_to_id": assignee,
                "sort": args.sort,
            }
            if tracker_value:
                params["tracker_id"] = tracker_value
            if project:
                params["project_id"] = project
            issues = fetch_all(
                client,
                "/issues.json",
                "issues",
                params=params,
                page_size=page_size,
                progress=True,
                max_results=limit,
            )
            for issue in issues:
                merged[issue.get("id")] = issue

    issues = list(merged.values())
    sort_issues(issues, args.sort)
    if limit:
        issues = issues[:limit]
    records = [issue_record(client, issue) for issue in issues]

    if args.format == "json":
        emit_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            args.output,
        )
        return
    if args.format == "csv":
        headers = ["id", "project", "tracker", "status", "subject", "url", "assigned_to"]
        rows = [
            [
                record["id"],
                record["project"],
                record["tracker"],
                record["status"],
                record["subject"],
                record["url"],
                record["assigned_to"],
            ]
            for record in records
        ]
        emit_text(csv_text(headers, rows), args.output, bom=True)
        return

    if not records:
        emit_text("沒有符合條件的 issue\n", args.output)
        return
    headers = ["ID", "Project", "Tracker", "Status", "Subject", "URL"]
    rows = [
        [
            record["id"],
            record["project"],
            record["tracker"],
            record["status"],
            record["subject"],
            record["url"],
        ]
        for record in records
    ]
    emit_text(
        render_table(
            headers,
            rows,
            max_widths=[6, 30, 14, 13, 60, 45],
            min_widths=[4, 12, 7, 7, 20, 24],
            alignments=["right", "left", "left", "left", "left", "left"],
        ),
        args.output,
    )


def parse_issue_include(raw):
    items = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    invalid = [item for item in items if item not in ISSUE_INCLUDE_CHOICES]
    if invalid:
        raise ValueError(
            "不支援的 --include 值: "
            + ", ".join(invalid)
            + "；可用: "
            + ", ".join(ISSUE_INCLUDE_CHOICES)
        )
    return list(dict.fromkeys(items))


def issue_include_arg(raw):
    try:
        return parse_issue_include(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def resolve_issue_include(items, no_comments):
    if isinstance(items, str):
        items = parse_issue_include(items)
    items = list(items or [])
    if no_comments:
        items = [item for item in items if item != "journals"]
    return items


def relation_display(relation, current_id):
    from_id = relation.get("issue_id")
    to_id = relation.get("issue_to_id")
    relation_type = relation.get("relation_type") or "relates"
    if current_id == from_id:
        return to_id, relation_type
    if current_id == to_id:
        return from_id, RELATION_TYPE_INVERSE.get(relation_type, relation_type)
    return to_id or from_id, relation_type


def render_issue_text(client, issue, include_items):
    lines = [
        f"ID: {issue.get('id')}",
        f"URL: {issue_url(client, issue)}",
        f"Project: {value_name(issue.get('project'))}",
        f"Tracker: {value_name(issue.get('tracker'))}",
        f"Status: {value_name(issue.get('status'))}",
        f"Priority: {value_name(issue.get('priority'))}",
        f"Assignee: {value_name(issue.get('assigned_to')) or '-'}",
        f"Author: {value_name(issue.get('author')) or '-'}",
        f"Progress: {issue.get('done_ratio') or 0}%",
        f"Created: {issue.get('created_on', '')}",
        f"Updated: {issue.get('updated_on', '')}",
    ]
    if issue.get("due_date"):
        lines.append(f"Due date: {issue['due_date']}")
    if issue.get("closed_on"):
        lines.append(f"Closed: {issue['closed_on']}")
    if issue.get("category"):
        lines.append(f"Category: {value_name(issue['category'])}")
    if issue.get("fixed_version"):
        lines.append(f"Version: {value_name(issue['fixed_version'])}")
    lines.extend(
        [
            "",
            f"Subject: {issue.get('subject', '')}",
            "",
            "Description:",
            issue.get("description") or "(empty)",
        ]
    )

    if "journals" in include_items:
        journals = issue.get("journals") or []
        comments = [
            journal
            for journal in journals
            if str(journal.get("notes") or "").strip()
        ]
        lines.extend(["", f"Comments ({len(comments)}):"])
        if not comments:
            lines.append("  (none)")
        for journal in comments:
            header = (
                f"  [{journal.get('created_on', '')}] "
                f"{value_name(journal.get('user')) or 'Unknown'}"
            )
            if journal.get("private_notes"):
                header += " (private)"
            lines.append(header)
            for note_line in str(journal.get("notes")).rstrip().splitlines():
                lines.append("    " + note_line)
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()

    if "attachments" in include_items:
        attachments = issue.get("attachments") or []
        lines.extend(["", f"Attachments ({len(attachments)}):"])
        if not attachments:
            lines.append("  (none)")
        for attachment in attachments:
            size = attachment.get("filesize")
            size_text = f" ({size} bytes)" if size not in (None, "") else ""
            lines.append(f"  {attachment.get('filename') or '(unnamed)'}{size_text}")
            if attachment.get("description"):
                lines.append(f"    {attachment['description']}")
            if attachment.get("content_url"):
                lines.append(f"    {attachment['content_url']}")

    if "relations" in include_items:
        relations = issue.get("relations") or []
        lines.extend(["", f"Relations ({len(relations)}):"])
        if not relations:
            lines.append("  (none)")
        for relation in relations:
            other_id, relation_type = relation_display(relation, issue.get("id"))
            lines.append(f"  #{other_id} ({relation_type})")

    return "\n".join(lines)


def command_issue(args, client):
    include_items = resolve_issue_include(args.include, args.no_comments)
    params = {"include": ",".join(include_items)} if include_items else None
    issue_ids = list(dict.fromkeys(args.issue_ids))
    show_progress = len(issue_ids) > 1
    issues = []
    missing = []
    try:
        for index, issue_id in enumerate(issue_ids, 1):
            if show_progress:
                print(
                    f"\r取得 issue {index}/{len(issue_ids)}...",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
            try:
                data = client.get_json(
                    f"/issues/{issue_id}.json",
                    params,
                    not_found=f"錯誤: 找不到 issue #{issue_id}",
                )
            except IssueNotFound as exc:
                missing.append(str(exc))
                continue
            issues.append(data.get("issue") or {})
    finally:
        if show_progress:
            print(file=sys.stderr)

    for message in missing:
        print(message, file=sys.stderr)
    if not issues:
        raise SystemExit(1)

    if args.format == "json":
        payloads = [dict(issue, url=issue_url(client, issue)) for issue in issues]
        payload = payloads[0] if len(payloads) == 1 else payloads
        emit_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            args.output,
        )
    else:
        rendered = [render_issue_text(client, issue, include_items) for issue in issues]
        emit_text("\n\n".join(rendered) + "\n", args.output)

    if missing:
        raise SystemExit(1)


def issue_resolved_by(issue, user_id, resolved_ids):
    for journal in issue.get("journals") or []:
        if (journal.get("user") or {}).get("id") != user_id:
            continue
        for detail in journal.get("details") or []:
            if (
                detail.get("property") == "attr"
                and detail.get("name") == "status_id"
                and str(detail.get("new_value")) in resolved_ids
            ):
                return True
    return False


def command_stats_by_resolution(
    args, client, output_dir, page_size, resolved_ids
):
    project_filter = None
    if args.project:
        project_filter = resolve_project(args.project, get_projects(client))
    assignee = resolve_assignee(
        args.assignee, client, output_dir, project_filter
    )
    if assignee == "me":
        user_id = get_current_user(client).get("id")
    else:
        user_id = int(assignee)
    if user_id is None:
        raise SystemExit("錯誤: 無法取得使用者 ID")

    params = {
        "updated_by": assignee,
        "status_id": "*",
        "sort": "id:asc",
    }
    if project_filter:
        params["project_id"] = project_filter
    issues = fetch_all(
        client,
        "/issues.json",
        "issues",
        params=params,
        page_size=page_size,
        progress=True,
    )
    print(
        f"檢查 {len(issues)} 筆 issue 的 journals（可能需要一些時間）...",
        file=sys.stderr,
    )

    buckets = {}
    for index, issue in enumerate(issues, 1):
        data = client.get_json(
            f"/issues/{issue['id']}.json",
            {"include": "journals"},
        )
        full_issue = data.get("issue") or {}
        if issue_resolved_by(full_issue, user_id, resolved_ids):
            project = full_issue.get("project") or issue.get("project") or {}
            key = (project.get("id"), str(project.get("name", "")).strip())
            bucket = buckets.setdefault(
                key,
                {
                    "project_id": project.get("id"),
                    "project": str(project.get("name", "")).strip(),
                    "resolved": 0,
                },
            )
            bucket["resolved"] += 1
        print(
            f"\r檢查 {index}/{len(issues)}",
            end="",
            file=sys.stderr,
            flush=True,
        )
    if issues:
        print(file=sys.stderr)

    records = sorted(
        buckets.values(),
        key=lambda record: (
            -record["resolved"],
            record["project"].casefold(),
        ),
    )
    if args.format == "json":
        emit_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            args.output,
        )
        return
    if args.format == "csv":
        headers = ["project_id", "project", "resolved"]
        rows = [
            [record["project_id"], record["project"], record["resolved"]]
            for record in records
        ]
        emit_text(csv_text(headers, rows), args.output, bom=True)
        return
    if not records:
        emit_text("沒有符合條件的 issue\n", args.output)
        return
    emit_text(
        render_table(
            ["Project", "Resolved"],
            [[record["project"], record["resolved"]] for record in records],
            max_widths=[50, 9],
            min_widths=[20, 8],
            alignments=["left", "right"],
        ),
        args.output,
    )


def command_stats(args, client, output_dir, page_size):
    statuses = get_statuses(client)
    resolved_ids = set(resolve_ids(statuses, args.resolved_status, "Status"))
    if args.by_resolution:
        command_stats_by_resolution(
            args, client, output_dir, page_size, resolved_ids
        )
        return
    closed_ids = {
        str(status.get("id")) for status in statuses if status.get("is_closed")
    }
    project_filter = None
    if args.project:
        project_filter = resolve_project(args.project, get_projects(client))
    assignee = resolve_assignee(
        args.assignee, client, output_dir, project_filter
    )
    params = {
        "assigned_to_id": assignee,
        "status_id": "*",
        "sort": "id:asc",
    }
    if project_filter:
        params["project_id"] = project_filter
    issues = fetch_all(
        client,
        "/issues.json",
        "issues",
        params=params,
        page_size=page_size,
        progress=True,
    )

    buckets = {}
    for issue in issues:
        project = issue.get("project") or {}
        key = (project.get("id"), str(project.get("name", "")).strip())
        bucket = buckets.setdefault(
            key,
            {
                "project_id": project.get("id"),
                "project": str(project.get("name", "")).strip(),
                "open": 0,
                "resolved": 0,
                "closed": 0,
                "total": 0,
            },
        )
        status_id = str((issue.get("status") or {}).get("id"))
        bucket["total"] += 1
        if status_id in resolved_ids:
            bucket["resolved"] += 1
        elif status_id in closed_ids:
            bucket["closed"] += 1
        else:
            bucket["open"] += 1

    records = sorted(
        buckets.values(),
        key=lambda record: (
            -record["resolved"],
            -record["open"],
            record["project"].casefold(),
        ),
    )

    if args.format == "json":
        emit_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            args.output,
        )
        return
    if args.format == "csv":
        headers = ["project_id", "project", "open", "resolved", "closed", "total"]
        rows = [
            [
                record["project_id"],
                record["project"],
                record["open"],
                record["resolved"],
                record["closed"],
                record["total"],
            ]
            for record in records
        ]
        emit_text(csv_text(headers, rows), args.output, bom=True)
        return

    if not records:
        emit_text("沒有符合條件的 issue\n", args.output)
        return
    headers = ["Project", "Open", "Resolved", "Closed", "Total"]
    rows = [
        [
            record["project"],
            record["open"],
            record["resolved"],
            record["closed"],
            record["total"],
        ]
        for record in records
    ]
    emit_text(
        render_table(
            headers,
            rows,
            max_widths=[50, 6, 9, 7, 7],
            min_widths=[20, 4, 8, 6, 5],
            alignments=["left", "right", "right", "right", "right"],
        ),
        args.output,
    )


def command_assignees(args, client, output_dir, page_size):
    issues = None if args.live else load_export_issues(output_dir)
    if issues is not None:
        print(
            f"使用 {Path(output_dir) / 'issues.json'}（{len(issues)} 筆）",
            file=sys.stderr,
        )
        statuses = get_statuses(client)
        closed_status_ids = {
            status.get("id") for status in statuses if status.get("is_closed")
        }
        people = collect_people(issues, closed_status_ids)
        write_assignee_cache(output_dir, client.base_url, people)
    elif args.live:
        print("從 API 讀取所有 issue 以建立 assignee 名單...", file=sys.stderr)
        issues = fetch_all(
            client,
            "/issues.json",
            "issues",
            params={"status_id": "*", "sort": "id:asc"},
            page_size=page_size,
            progress=True,
        )
        statuses = get_statuses(client)
        closed_status_ids = {
            status.get("id") for status in statuses if status.get("is_closed")
        }
        people = collect_people(issues, closed_status_ids)
        write_assignee_cache(output_dir, client.base_url, people)
    else:
        people = load_assignee_cache(output_dir, client.base_url)
        if people is None:
            raise SystemExit(
                "錯誤: 找不到 assignee 名單。\n"
                "請先執行下列其中一個指令：\n"
                "  ./redmine_cli.py assignees --live\n"
                "  ./redmine_cli.py export"
            )
        print(
            f"使用 {assignee_cache_path(output_dir)} 快取（{len(people)} 人）",
            file=sys.stderr,
        )
    if args.sort == "name":
        people.sort(key=lambda person: person["name"].casefold())
    else:
        people.sort(
            key=lambda person: (-person[args.sort], person["name"].casefold())
        )

    if args.format == "json":
        emit_text(
            json.dumps(people, ensure_ascii=False, indent=2) + "\n",
            args.output,
        )
        return
    if args.format == "csv":
        headers = ["id", "name", "account", "total", "open"]
        rows = [
            [
                person["id"],
                person["name"],
                assignee_account(person),
                person["total"],
                person["open"],
            ]
            for person in people
        ]
        emit_text(csv_text(headers, rows), args.output, bom=True)
        return
    headers = ["ID", "Name", "Account", "Total", "Open"]
    rows = [
        [
            person["id"],
            person["name"],
            assignee_account(person),
            person["total"],
            person["open"],
        ]
        for person in people
    ]
    emit_text(
        render_table(
            headers,
            rows,
            max_widths=[8, 30, 24, 7, 7],
            min_widths=[3, 12, 8, 3, 3],
            alignments=["right", "left", "left", "right", "right"],
        ),
        args.output,
    )


def command_meta(args, client):
    trackers = get_trackers(client)
    statuses = get_statuses(client)
    user = get_current_user(client)
    user_info = {
        "id": user.get("id"),
        "login": user.get("login"),
        "firstname": user.get("firstname"),
        "lastname": user.get("lastname"),
        "admin": user.get("admin"),
    }
    if args.format == "json":
        emit_text(
            json.dumps(
                {
                    "current_user": user_info,
                    "trackers": trackers,
                    "statuses": statuses,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            args.output,
        )
        return

    lines = [
        f"Current user: {user_info['firstname']} {user_info['lastname']} "
        f"<{user_info['login']}> (id={user_info['id']}, admin={user_info['admin']})",
        "",
        f"Trackers ({len(trackers)})",
        render_table(
            ["ID", "Name"],
            [[tracker.get("id"), tracker.get("name")] for tracker in trackers],
            max_widths=[8, 40],
            min_widths=[3, 10],
            alignments=["right", "left"],
        ).rstrip(),
        "",
        f"Statuses ({len(statuses)})",
        render_table(
            ["ID", "Name", "Closed"],
            [
                [
                    status.get("id"),
                    status.get("name"),
                    "yes" if status.get("is_closed") else "no",
                ]
                for status in statuses
            ],
            max_widths=[8, 40, 8],
            min_widths=[3, 10, 6],
            alignments=["right", "left", "left"],
        ).rstrip(),
        "",
    ]
    emit_text("\n".join(lines), args.output)


def command_export(args, client, script_dir):
    project = None
    if args.project:
        project = resolve_project(args.project, get_projects(client))
    if args.dry_run:
        dry_run(client, project, args.project)
        return

    page_size = max(1, min(args.page_size, 100))
    issues = fetch_issues(client, project, page_size)
    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else script_dir / "output"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.format in ("json", "both"):
        path = output_dir / "issues.json"
        write_json(path, issues)
        print(f"已寫入 {path}（{len(issues)} 筆）")

    if args.format in ("csv", "both"):
        path = output_dir / "issues.csv"
        write_csv(path, issues, max(0, args.csv_description_limit))
        print(f"已寫入 {path}（{len(issues)} 筆）")


def add_connection_args(parser):
    group = parser.add_argument_group("連線與驗證（通常不用）")
    group.add_argument("--url", help="預設讀 .env 的 REDMINE_URL；要臨時換站台才加")
    group.add_argument(
        "--api-key-file",
        help="預設讀 .env 或 REDMINE_API_KEY；要從檔案讀金鑰才加",
    )
    group.add_argument(
        "--env-file",
        help="預設讀程式目錄的 .env；要用其他設定檔才加",
    )
    group.add_argument(
        "--insecure",
        action="store_true",
        help="通常不用；只有 TLS 憑證驗證失敗時才加",
    )
    group.add_argument(
        "--ca-cert",
        help="通常不用；有 CA bundle 時優先使用這個，而不是 --insecure",
    )
    group.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="通常不用；預設 30 秒",
    )
    group.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="通常不用；預設失敗重試 4 次",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        usage=(
            "%(prog)s [<指令>] [選項]\n"
            "\n"
            "指令:\n"
            "  (不加指令)                          我的未關閉 issue\n"
            "                                       = issues --assignee me --status open\n"
            "\n"
            "  issues\n"
            "  |-- --assignee <me|ID|帳號|姓名>    看別人的 issue\n"
            "  |-- --status <open|closed|all|名稱>  狀態\n"
            "  |-- --project <ID|名稱>              只看某專案\n"
            "  `-- 其他選項見 issues --help\n"
            "\n"
            "  projects\n"
            "  |-- --summary                        上層專案摘要\n"
            "  |-- --project <ID|名稱>              只看某專案\n"
            "  |-- --hide-empty                     隱藏數量為 0\n"
            "  `-- 其他選項見 projects --help\n"
            "\n"
            "  issue <ID> [<ID> ...]               一或多個 issue 內容、進度、留言\n"
            "  |-- --include <LIST>                 預設 journals；可加 attachments,relations\n"
            "  |-- --no-comments\n"
            "  |-- --format <text|json>\n"
            "  `-- 其他選項見 issue --help\n"
            "\n"
            "  stats\n"
            "  |-- --assignee <me|ID|帳號|姓名>    統計對象\n"
            "  |-- --by-resolution                  依 journal 計算曾解決\n"
            "  |-- --resolved-status <名稱|ID>\n"
            "  `-- 其他選項見 stats --help\n"
            "\n"
            "  assignees                            列出 assignee\n"
            "  `-- 其他選項見 assignees --help\n"
            "\n"
            "  meta                                 列出 Tracker / Status\n"
            "  `-- 其他選項見 meta --help\n"
            "\n"
            "  export                               匯出全部 issue\n"
            "  `-- 其他選項見 export --help"
        ),
        epilog=(
            "常用範例:\n"
            "  %(prog)s                                   我的未關閉 issue\n"
            "  %(prog)s issues --assignee \"USER_NAME\"      看別人的 issue\n"
            "  %(prog)s issue 12345                       單一 issue 內容、進度、留言\n"
            "  %(prog)s issue 12345 67890 --format json   多筆 issue JSON\n"
            "  %(prog)s projects --summary                上層專案摘要\n"
            "  %(prog)s stats --by-resolution             我曾經解決的數量\n"
            "  %(prog)s assignees --live                  重建 assignee 名單\n"
            "  %(prog)s meta                              列出 Tracker / Status\n"
            "  %(prog)s export --format both              匯出全部 issue\n"
            "\n"
            "不加指令時 = issues --assignee me --status open（只列未關閉）。\n"
            "符號: <...> 必要、[...] 可選、A | B 擇一\n"
            "姓名解析使用 output/assignees.json；可用 assignees --live 建立。\n"
            "所有指令都可用連線選項；詳見 <指令> --help。\n"
            "\n"
            "設定:\n"
            "  建立 .env（參考 .env.example），填入 REDMINE_URL 與 REDMINE_API_KEY。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help=argparse.SUPPRESS)

    export_parser = subparsers.add_parser(
        "export",
        prog=f"{parser.prog} export",
        usage=(
            "%(prog)s [--dry-run] [--project <名稱|ID>]\n"
            "       [--format json|csv|both] [--output-dir DIR]"
        ),
        help="匯出全部 issue 為 JSON/CSV",
        description="匯出全部 issue 為 JSON/CSV。預設輸出到 output/。",
        epilog=(
            "範例:\n"
            "  %(prog)s --dry-run\n"
            "  %(prog)s --format both\n"
            "  %(prog)s --project my-project\n"
            "  %(prog)s --output-dir /tmp/redmine-export"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    export_group = export_parser.add_argument_group("匯出選項")
    export_group.add_argument(
        "--project",
        help="預設全部專案；只匯出某專案才加（名稱/identifier/ID）",
    )
    export_group.add_argument(
        "--output-dir",
        help="預設程式目錄的 output/；要換位置才加",
    )
    export_group.add_argument(
        "--format",
        choices=("json", "csv", "both"),
        default="both",
        help="預設 both；只想要一種格式才加",
    )
    export_group.add_argument(
        "--dry-run",
        action="store_true",
        help="只查總筆數，不寫檔；通常不用",
    )
    export_group.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="通常不用；預設每頁 100 筆",
    )
    export_group.add_argument(
        "--csv-description-limit",
        type=int,
        default=32000,
        help="通常不用；CSV description 預設截斷至 32000 字元",
    )
    add_connection_args(export_parser)

    projects_parser = subparsers.add_parser(
        "projects",
        prog=f"{parser.prog} projects",
        usage=(
            "%(prog)s [--summary | --format table|detail|tree|flat|json|csv | --flat]\n"
            "       [--project <名稱|ID>] [--hide-empty] [--no-count]"
        ),
        help="列出所有專案與未關閉 issue 數",
        description=(
            "列出所有可見專案。預設顯示完整階層表格，"
            "並標示未關閉 issue 數。"
        ),
        epilog=(
            "範例:\n"
            "  %(prog)s                    完整階層表格\n"
            "  %(prog)s --summary          上層專案摘要\n"
            "  %(prog)s --hide-empty       隱藏數量為 0 的專案\n"
            "  %(prog)s --format detail    含 ID/identifier/parent 的明細表\n"
            "  %(prog)s --project \"Project A\" --summary\n"
            "  %(prog)s --format json --output projects.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    project_display = projects_parser.add_argument_group("顯示方式")
    project_mode = project_display.add_mutually_exclusive_group()
    project_mode.add_argument(
        "--summary",
        action="store_true",
        help="只列上層專案；和 --format / --flat 擇一",
    )
    project_mode.add_argument(
        "--format",
        choices=("table", "detail", "tree", "flat", "json", "csv"),
        default="table",
        help="預設 table；要明細/JSON/CSV 才加",
    )
    project_mode.add_argument(
        "--flat",
        action="store_true",
        help="緊湊清單；和 --summary / --format 擇一",
    )
    project_filter = projects_parser.add_argument_group("篩選")
    project_filter.add_argument(
        "--project",
        help="預設全部專案；只看某專案及其子專案才加",
    )
    project_filter.add_argument(
        "--hide-empty",
        action="store_true",
        help="預設顯示 0；要精簡輸出才加",
    )
    project_filter.add_argument(
        "--no-count",
        action="store_true",
        help="預設會統計；要跳過計數、加快速度才加",
    )
    project_output = projects_parser.add_argument_group("輸出")
    project_output.add_argument("--output", help="預設印在螢幕；要存檔才加")
    add_connection_args(projects_parser)

    issues_parser = subparsers.add_parser(
        "issues",
        prog=f"{parser.prog} issues",
        usage=(
            "%(prog)s [--assignee <me|ID|ACCOUNT|NAME>]\n"
            "       [--status <open|closed|all|NAME>] [--project <NAME|ID>]\n"
            "       [--format table|json|csv]"
        ),
        help="列出指派給指定人員的 issue",
        description=(
            "列出指派給指定人員的 issue。\n"
            "不加任何選項時：assignee=me、status=open。"
        ),
        epilog=(
            "範例:\n"
            "  %(prog)s\n"
            "  %(prog)s --assignee 123\n"
            "  %(prog)s --assignee \"USER_NAME\"\n"
            "  %(prog)s --status all\n"
            "  %(prog)s --tracker Bug --status \"New,In Progress\"\n"
            "  %(prog)s --project \"Project A\"\n"
            "  %(prog)s --format csv --output my_issues.csv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    issue_query = issues_parser.add_argument_group("查詢條件")
    issue_query.add_argument(
        "--assignee",
        default="me",
        help="預設 me；可用數字 ID、登入帳號或顯示名稱；帳號/姓名需本機名單",
    )
    issue_query.add_argument(
        "--status",
        default="open",
        help="預設 open；不加只列未關閉，要含已關閉用 all",
    )
    issue_query.add_argument(
        "--tracker",
        help="預設全部 Tracker；只看特定 Tracker 才加",
    )
    issue_query.add_argument(
        "--project",
        help="預設全部專案；只看某專案才加",
    )
    issue_order = issues_parser.add_argument_group("排序與筆數")
    issue_order.add_argument(
        "--sort",
        default="updated_on:desc",
        help="預設 updated_on:desc；要改排序才加",
    )
    issue_order.add_argument(
        "--limit",
        type=int,
        default=0,
        help="預設 0（全部）；要限制筆數才加",
    )
    issue_output = issues_parser.add_argument_group("輸出")
    issue_output.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="預設 table；要 JSON/CSV 才加",
    )
    issue_output.add_argument("--output", help="預設印在螢幕；要存檔才加")
    add_connection_args(issues_parser)

    issue_parser = subparsers.add_parser(
        "issue",
        prog=f"{parser.prog} issue",
        usage=(
            "%(prog)s <ID> [<ID> ...] [--no-comments] [--include LIST]\n"
            "       [--format text|json]"
        ),
        help="顯示一或多個 issue 的內容、進度與留言",
        description=(
            "顯示一或多個 issue 的欄位、進度、指派者與留言。\n"
            "不加任何選項時：包含 journals（留言）。\n"
            "多個 ID 時 JSON 輸出為陣列；單一 ID 維持物件。"
        ),
        epilog=(
            "範例:\n"
            "  %(prog)s 12345\n"
            "  %(prog)s 12345 67890\n"
            "  %(prog)s 12345 --no-comments\n"
            "  %(prog)s 12345 --include journals,attachments,relations\n"
            "  %(prog)s 12345 --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    issue_parser.add_argument(
        "issue_ids",
        type=int,
        nargs="+",
        metavar="ID",
        help="必要參數：一或多個 Issue ID",
    )
    issue_show = issue_parser.add_argument_group("顯示方式")
    issue_show.add_argument(
        "--no-comments",
        action="store_true",
        help="預設顯示留言；不想看留言才加（從 include 移除 journals）",
    )
    issue_show.add_argument(
        "--include",
        type=issue_include_arg,
        default=["journals"],
        metavar="LIST",
        help=(
            "預設 journals；逗號分隔，可用: "
            + ",".join(ISSUE_INCLUDE_CHOICES)
        ),
    )
    issue_show_output = issue_parser.add_argument_group("輸出")
    issue_show_output.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="預設 text；要 JSON 才加",
    )
    issue_show_output.add_argument("--output", help="預設印在螢幕；要存檔才加")
    add_connection_args(issue_parser)

    stats_parser = subparsers.add_parser(
        "stats",
        prog=f"{parser.prog} stats",
        usage=(
            "%(prog)s [--assignee <me|ID|ACCOUNT|NAME>] [--by-resolution]\n"
            "       [--project <NAME|ID>] [--resolved-status <NAME|ID>]\n"
            "       [--format table|json|csv]"
        ),
        help="統計指定人員目前在各專案的 issue 狀態",
        description=(
            "預設統計目前指派給指定人員的 issue 在各專案的 "
            "Open / Resolved / Closed 數量。\n"
            "加上 --by-resolution 則改用 journal 狀態變更，"
            "統計該人員曾將 issue 改為 Resolved 的數量。"
        ),
        epilog=(
            "範例:\n"
            "  %(prog)s --assignee me\n"
            "  %(prog)s --assignee me --by-resolution\n"
            "  %(prog)s --assignee 123\n"
            "  %(prog)s --assignee me --project \"Project A\"\n"
            "  %(prog)s --assignee me --resolved-status \"Resolved,Closed\""
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stats_query = stats_parser.add_argument_group("統計條件")
    stats_query.add_argument(
        "--assignee",
        default="me",
        help="預設 me；可用數字 ID、登入帳號或顯示名稱；帳號/姓名需本機名單",
    )
    stats_query.add_argument(
        "--project",
        help="預設全部專案；只看某專案才加",
    )
    stats_query.add_argument(
        "--by-resolution",
        action="store_true",
        help="預設用目前 assignee；要依 journal 算「曾解決」才加",
    )
    stats_query.add_argument(
        "--resolved-status",
        default="Resolved",
        help="預設 Resolved；要改認定狀態才加",
    )
    stats_output = stats_parser.add_argument_group("輸出")
    stats_output.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="預設 table；要 JSON/CSV 才加",
    )
    stats_output.add_argument("--output", help="預設印在螢幕；要存檔才加")
    add_connection_args(stats_parser)

    assignees_parser = subparsers.add_parser(
        "assignees",
        prog=f"{parser.prog} assignees",
        usage="%(prog)s [--sort total|open|name] [--live] [--format table|json|csv]",
        help="列出 issue 的 assignee",
        description=(
            "列出 issue 中出現過的 assignee（含帳號別名），並更新 output/assignees.json 快取。\n"
            "不加任何選項時：依序讀取 output/issues.json 或 output/assignees.json。"
        ),
        epilog=(
            "範例:\n"
            "  %(prog)s\n"
            "  %(prog)s --sort open\n"
            "  %(prog)s --live\n"
            "  %(prog)s --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    assignees_show = assignees_parser.add_argument_group("顯示方式")
    assignees_show.add_argument(
        "--sort",
        choices=("total", "open", "name"),
        default="total",
        help="預設 total；要改排序才加",
    )
    assignees_show.add_argument(
        "--live",
        action="store_true",
        help="預設讀本機名單；要從 API 重建並更新 assignees.json 才加",
    )
    assignees_output = assignees_parser.add_argument_group("輸出")
    assignees_output.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="預設 table；要 JSON/CSV 才加",
    )
    assignees_output.add_argument("--output", help="預設印在螢幕；要存檔才加")
    add_connection_args(assignees_parser)

    meta_parser = subparsers.add_parser(
        "meta",
        prog=f"{parser.prog} meta",
        usage="%(prog)s [--format text|json]",
        help="列出 Tracker、Status 與目前使用者",
        description="列出 Tracker、Status 與目前使用者。",
        epilog=(
            "範例:\n"
            "  %(prog)s\n"
            "  %(prog)s --format json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    meta_output = meta_parser.add_argument_group("輸出")
    meta_output.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="預設 text；要 JSON 才加",
    )
    meta_output.add_argument("--output", help="預設印在螢幕；要存檔才加")
    add_connection_args(meta_parser)

    parser.command_parsers = {
        "export": export_parser,
        "projects": projects_parser,
        "issues": issues_parser,
        "issue": issue_parser,
        "stats": stats_parser,
        "assignees": assignees_parser,
        "meta": meta_parser,
    }
    return parser


def normalize_argv(argv):
    if not argv:
        return ["issues"]
    if argv[0] in COMMANDS or argv[0] in ("-h", "--help"):
        return argv
    flags = {argument.split("=", 1)[0] for argument in argv}
    if flags & EXPORT_ONLY_FLAGS:
        return ["export"] + argv
    for index, argument in enumerate(argv):
        if argument == "--format=both":
            return ["export"] + argv
        if (
            argument == "--format"
            and index + 1 < len(argv)
            and argv[index + 1] == "both"
        ):
            return ["export"] + argv
    return ["issues"] + argv


def main():
    parser = build_parser()
    raw_args = sys.argv[1:]
    defaulted = not raw_args
    args, unknown = parser.parse_known_args(normalize_argv(raw_args))
    if unknown:
        explicit_command = bool(raw_args) and raw_args[0] in COMMANDS
        target_parser = parser
        if explicit_command:
            target_parser = parser.command_parsers.get(args.command, parser)
        target_parser.print_help(sys.stderr)
        print(
            f"\n錯誤: 無法辨識的參數: {' '.join(unknown)}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not getattr(args, "command", None):
        parser.print_help()
        return

    script_dir = Path(__file__).resolve().parent
    env_path = Path(args.env_file).expanduser() if args.env_file else script_dir / ".env"
    load_env_file(env_path)
    if env_path.exists():
        check_permissions(env_path)

    base_url = args.url or os.environ.get("REDMINE_URL")
    if not base_url:
        raise SystemExit(
            "錯誤: 未設定 Redmine 網址。請建立 .env（參考 .env.example）、"
            "設定 REDMINE_URL，或加上 --url。"
        )
    insecure = args.insecure or env_bool("REDMINE_INSECURE", False)
    ca_cert = None
    if args.ca_cert:
        ca_path = Path(args.ca_cert).expanduser()
        if not ca_path.exists():
            raise SystemExit(f"錯誤: CA bundle 不存在: {ca_path}")
        ca_cert = str(ca_path)
    api_key = resolve_api_key(args)
    page_size = max(1, min(getattr(args, "page_size", 100), 100))

    if insecure and not ca_cert:
        print(
            "警告: 已停用 TLS 憑證驗證 (--insecure)，僅建議用於受信任的內網。",
            file=sys.stderr,
        )

    client = RedmineClient(
        base_url,
        api_key,
        insecure,
        args.timeout,
        max(1, args.max_retries),
        ca_cert=ca_cert,
    )
    output_dir = script_dir / "output"

    if args.command == "export":
        command_export(args, client, script_dir)
    elif args.command == "projects":
        command_projects(args, client)
    elif args.command == "issues":
        command_issues(args, client, output_dir, page_size)
        if defaulted:
            sys.stdout.flush()
            print(
                "提示: 不加指令等同 issues --assignee me --status open；"
                "--help 可看其他指令。",
                file=sys.stderr,
            )
    elif args.command == "issue":
        command_issue(args, client)
    elif args.command == "stats":
        command_stats(args, client, output_dir, page_size)
    elif args.command == "assignees":
        command_assignees(args, client, output_dir, page_size)
    elif args.command == "meta":
        command_meta(args, client)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        raise SystemExit(130)
