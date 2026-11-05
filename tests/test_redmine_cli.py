import argparse
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import redmine_cli as r


def make_issue(issue_id):
    return {
        "id": issue_id,
        "project": {"id": 1, "name": "Project A"},
        "tracker": {"id": 2, "name": "Bug"},
        "status": {"id": 3, "name": "In Progress"},
        "priority": {"id": 4, "name": "Normal"},
        "subject": f"Subject {issue_id}",
        "description": "Description",
        "done_ratio": 40,
        "created_on": "2026-01-01T00:00:00Z",
        "updated_on": "2026-01-02T00:00:00Z",
        "author": {"id": 5, "name": "Author"},
        "assigned_to": {"id": 6, "name": "Assignee"},
        "journals": [
            {
                "notes": "A comment",
                "user": {"id": 5, "name": "Author"},
                "created_on": "2026-01-02T00:00:00Z",
            }
        ],
        "attachments": [
            {
                "filename": "note.txt",
                "filesize": 12,
                "content_url": "https://redmine.example/attachments/1",
            }
        ],
        "relations": [
            {
                "issue_id": issue_id,
                "issue_to_id": 99,
                "relation_type": "blocks",
            }
        ],
    }


class FakeClient:
    base_url = "https://redmine.example"

    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    def get_json(self, path, params=None, not_found=None):
        self.calls.append((path, params))
        issue_id = int(path.split("/")[2].split(".")[0])
        if issue_id not in self.issues:
            raise r.IssueNotFound(not_found or f"missing {issue_id}")
        return {"issue": self.issues[issue_id]}


def make_args(**overrides):
    values = {
        "include": ["journals"],
        "no_comments": False,
        "issue_ids": [1],
        "format": "text",
        "output": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class IncludeParsingTests(unittest.TestCase):
    def test_parse_dedupes_and_strips(self):
        self.assertEqual(
            r.parse_issue_include(" journals, attachments ,journals "),
            ["journals", "attachments"],
        )

    def test_parse_empty(self):
        self.assertEqual(r.parse_issue_include(""), [])
        self.assertEqual(r.parse_issue_include(None), [])

    def test_parse_rejects_unknown(self):
        with self.assertRaises(ValueError) as ctx:
            r.parse_issue_include("journals,bogus")
        self.assertIn("bogus", str(ctx.exception))

    def test_argparse_type_rejects_unknown(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            r.issue_include_arg("bogus")

    def test_no_comments_removes_journals(self):
        self.assertEqual(
            r.resolve_issue_include("journals,attachments", True),
            ["attachments"],
        )

    def test_no_comments_without_journals(self):
        self.assertEqual(
            r.resolve_issue_include(["attachments"], True),
            ["attachments"],
        )

    def test_resolve_none(self):
        self.assertEqual(r.resolve_issue_include(None, False), [])


class RenderIssueTextTests(unittest.TestCase):
    def test_renders_requested_sections(self):
        text = r.render_issue_text(
            FakeClient({}),
            make_issue(1),
            ["journals", "attachments", "relations"],
        )
        self.assertIn("Comments (1):", text)
        self.assertIn("Attachments (1):", text)
        self.assertIn("note.txt (12 bytes)", text)
        self.assertIn("Relations (1):", text)
        self.assertIn("#99 (blocks)", text)

    def test_relation_uses_other_issue_id(self):
        issue = make_issue(123)
        issue["relations"] = [
            {
                "issue_id": 456,
                "issue_to_id": 123,
                "relation_type": "relates",
            }
        ]
        text = r.render_issue_text(FakeClient({}), issue, ["relations"])
        self.assertIn("#456 (relates)", text)
        self.assertNotIn("#123 (relates)", text)

    def test_relation_type_inverted_when_current_is_target(self):
        issue = make_issue(1)
        issue["relations"] = [
            {
                "issue_id": 10,
                "issue_to_id": 1,
                "relation_type": "blocks",
            }
        ]
        text = r.render_issue_text(FakeClient({}), issue, ["relations"])
        self.assertIn("#10 (blocked)", text)

    def test_relation_type_kept_when_current_is_source(self):
        issue = make_issue(1)
        issue["relations"] = [
            {
                "issue_id": 1,
                "issue_to_id": 10,
                "relation_type": "blocks",
            }
        ]
        text = r.render_issue_text(FakeClient({}), issue, ["relations"])
        self.assertIn("#10 (blocks)", text)

    def test_skips_unrequested_sections(self):
        text = r.render_issue_text(FakeClient({}), make_issue(1), [])
        self.assertNotIn("Comments", text)
        self.assertNotIn("Attachments", text)
        self.assertNotIn("Relations", text)

    def test_empty_sections_show_none(self):
        issue = make_issue(1)
        issue["attachments"] = []
        issue["relations"] = []
        text = r.render_issue_text(
            FakeClient({}), issue, ["attachments", "relations"]
        )
        self.assertIn("Attachments (0):\n  (none)", text)
        self.assertIn("Relations (0):\n  (none)", text)

    def test_null_done_ratio_shows_zero(self):
        issue = make_issue(1)
        issue["done_ratio"] = None
        text = r.render_issue_text(FakeClient({}), issue, [])
        self.assertIn("Progress: 0%", text)


class IssueRecordTests(unittest.TestCase):
    def test_contains_expected_fields(self):
        record = r.issue_record(FakeClient({}), make_issue(7))
        self.assertEqual(record["id"], 7)
        self.assertEqual(record["project_id"], 1)
        self.assertEqual(record["tracker_id"], 2)
        self.assertEqual(record["status_id"], 3)
        self.assertEqual(record["priority"], "Normal")
        self.assertEqual(record["author"], "Author")
        self.assertEqual(record["done_ratio"], 40)
        self.assertEqual(record["url"], "https://redmine.example/issues/7")

    def test_null_done_ratio_becomes_zero(self):
        issue = make_issue(7)
        issue["done_ratio"] = None
        self.assertEqual(r.issue_record(FakeClient({}), issue)["done_ratio"], 0)


class CommandIssueTests(unittest.TestCase):
    def run_command(self, args, client):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            r.command_issue(args, client)
        return stdout.getvalue(), stderr.getvalue()

    def test_single_json_is_object(self):
        client = FakeClient({1: make_issue(1)})
        out, _ = self.run_command(make_args(format="json"), client)
        payload = json.loads(out)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["url"], "https://redmine.example/issues/1")
        self.assertEqual(client.calls[0][1], {"include": "journals"})

    def test_multi_json_is_array_and_dedupes(self):
        client = FakeClient({1: make_issue(1), 2: make_issue(2)})
        out, _ = self.run_command(
            make_args(issue_ids=[2, 1, 2], format="json"), client
        )
        payload = json.loads(out)
        self.assertIsInstance(payload, list)
        self.assertEqual([item["id"] for item in payload], [2, 1])
        self.assertEqual([call[0] for call in client.calls], [
            "/issues/2.json",
            "/issues/1.json",
        ])

    def test_no_comments_omits_include_param(self):
        client = FakeClient({1: make_issue(1)})
        self.run_command(make_args(no_comments=True), client)
        self.assertIsNone(client.calls[0][1])

    def test_multi_text_shows_progress_and_separates_issues(self):
        client = FakeClient({1: make_issue(1), 2: make_issue(2)})
        out, err = self.run_command(make_args(issue_ids=[1, 2]), client)
        self.assertIn("ID: 1", out)
        self.assertIn("ID: 2", out)
        self.assertIn("\n\n", out)
        self.assertIn("取得 issue 2/2", err)

    def test_partial_missing_outputs_success_and_exits_nonzero(self):
        client = FakeClient({1: make_issue(1)})
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                r.command_issue(make_args(issue_ids=[1, 999]), client)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("ID: 1", stdout.getvalue())
        self.assertIn("找不到 issue #999", stderr.getvalue())

    def test_all_missing_exits_nonzero_without_output(self):
        client = FakeClient({})
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                r.command_issue(make_args(issue_ids=[998, 999]), client)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("找不到 issue #998", stderr.getvalue())
        self.assertIn("找不到 issue #999", stderr.getvalue())


def make_project(project_id, identifier, name):
    return {"id": project_id, "identifier": identifier, "name": name}


def make_status(status_id, name, is_closed=False):
    return {"id": status_id, "name": name, "is_closed": is_closed}


def make_tracker(tracker_id, name):
    return {"id": tracker_id, "name": name}


def make_user(user_id, name, login=""):
    return {"id": user_id, "name": name, "login": login}


def make_issue_in_project(
    issue_id,
    project,
    status=None,
    tracker=None,
    assignee=None,
    journals=None,
    updated_on="2026-01-02T00:00:00Z",
):
    return {
        "id": issue_id,
        "project": project,
        "tracker": tracker or make_tracker(1, "Bug"),
        "status": status or make_status(1, "New"),
        "priority": {"id": 2, "name": "Normal"},
        "subject": f"Subject {issue_id}",
        "description": "Description",
        "done_ratio": 0,
        "created_on": "2026-01-01T00:00:00Z",
        "updated_on": updated_on,
        "author": make_user(1, "Author"),
        "assigned_to": assignee or make_user(12, "Jane Doe", "jane_doe"),
        "journals": journals or [],
    }


class FakeRedmineClient:
    base_url = "https://redmine.example"

    def __init__(
        self,
        projects,
        issues,
        statuses=None,
        trackers=None,
        current_user=None,
        memberships=None,
        users=None,
        users_forbidden=False,
    ):
        self.projects = projects
        self.issues = issues
        self.statuses = statuses or [make_status(1, "New")]
        self.trackers = trackers or [make_tracker(1, "Bug")]
        self.current_user = current_user or make_user(12, "Jane Doe", "jane_doe")
        self.memberships = memberships or {}
        self.users = users or []
        self.users_forbidden = users_forbidden
        self.calls = []

    def _paginate(self, items, params):
        params = params or {}
        limit = int(params.get("limit", 100))
        offset = int(params.get("offset", 0))
        return items[offset : offset + limit], len(items)

    def _match_project(self, issue, project_value):
        project = issue.get("project") or {}
        return (
            str(project.get("id")) == str(project_value)
            or str(project.get("identifier", "")) == str(project_value)
            or str(project.get("name", "")).casefold()
            == str(project_value).casefold()
        )

    def _issues(self, params):
        params = params or {}
        items = list(self.issues)
        project_value = params.get("project_id")
        if project_value:
            items = [
                item for item in items if self._match_project(item, project_value)
            ]
        status_value = params.get("status_id")
        if status_value and status_value not in ("*", "all"):
            if status_value == "open":
                items = [item for item in items if not item["status"].get("is_closed")]
            elif status_value == "closed":
                items = [item for item in items if item["status"].get("is_closed")]
            else:
                items = [
                    item
                    for item in items
                    if str(item["status"].get("id")) == str(status_value)
                    or item["status"].get("name") == status_value
                ]
        assigned = params.get("assigned_to_id")
        if assigned == "me":
            user_id = self.current_user.get("id")
            items = [
                item
                for item in items
                if str((item.get("assigned_to") or {}).get("id")) == str(user_id)
            ]
        elif assigned:
            items = [
                item
                for item in items
                if str((item.get("assigned_to") or {}).get("id")) == str(assigned)
            ]
        tracker = params.get("tracker_id")
        if tracker:
            items = [
                item
                for item in items
                if str(item["tracker"].get("id")) == str(tracker)
            ]
        items.sort(key=lambda item: item["id"])
        batch, total = self._paginate(items, params)
        return {"issues": batch, "total_count": total}

    def get_json(self, path, params=None, not_found=None):
        self.calls.append((path, dict(params or {})))
        if path == "/projects.json":
            batch, total = self._paginate(self.projects, params)
            return {"projects": batch, "total_count": total}
        if path == "/issue_statuses.json":
            return {"issue_statuses": self.statuses}
        if path == "/trackers.json":
            return {"trackers": self.trackers}
        if path == "/users/current.json":
            return {"user": self.current_user}
        if path == "/users.json":
            if self.users_forbidden:
                raise SystemExit("錯誤: 403 權限不足，該帳號無法讀取此範圍的資料")
            name = str((params or {}).get("name", "")).casefold()
            matches = [
                user
                for user in self.users
                if not name
                or name in str(user.get("login", "")).casefold()
                or name in str(user.get("name", "")).casefold()
            ]
            return {"users": matches, "total_count": len(matches)}
        if path == "/issues.json":
            return self._issues(params)
        if path.startswith("/projects/") and path.endswith("/memberships.json"):
            project_value = path.split("/")[2]
            memberships = self.memberships.get(project_value, [])
            batch, total = self._paginate(memberships, params)
            return {"memberships": batch, "total_count": total}
        if path.startswith("/issues/") and path.endswith(".json"):
            issue_id = int(path.split("/")[2].split(".")[0])
            issue = next(
                (item for item in self.issues if item["id"] == issue_id), None
            )
            if issue is None:
                raise r.IssueNotFound(not_found or f"missing {issue_id}")
            return {"issue": issue}
        raise SystemExit(f"錯誤: 404 找不到 API: {path}")


def issues_args(**overrides):
    values = {
        "status": "open",
        "tracker": None,
        "project": None,
        "assignee": "me",
        "limit": 0,
        "sort": "id:asc",
        "format": "json",
        "output": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def stats_args(**overrides):
    values = {
        "assignee": "me",
        "project": None,
        "resolved_status": "Resolved",
        "by_resolution": False,
        "format": "json",
        "output": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def export_args(**overrides):
    values = {
        "project": None,
        "dry_run": False,
        "page_size": 100,
        "format": "json",
        "output_dir": None,
        "csv_description_limit": 32000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ResolveProjectsTests(unittest.TestCase):
    def setUp(self):
        self.projects = [
            make_project(101, "project-a", "Project A"),
            make_project(102, "project-b", "Project B"),
            make_project(103, "project-c", "Project C"),
        ]

    def test_single_project_name(self):
        self.assertEqual(
            r.resolve_projects("Project A", self.projects), ["project-a"]
        )

    def test_multiple_project_names(self):
        self.assertEqual(
            r.resolve_projects(
                "Project A, Project B,Project C", self.projects
            ),
            ["project-a", "project-b", "project-c"],
        )

    def test_mixed_name_identifier_and_id(self):
        self.assertEqual(
            r.resolve_projects("Project A,project-b,103", self.projects),
            ["project-a", "project-b", "project-c"],
        )

    def test_dedupes_same_project(self):
        self.assertEqual(
            r.resolve_projects("Project A,project-a,101", self.projects),
            ["project-a"],
        )

    def test_empty_returns_empty_list(self):
        self.assertEqual(r.resolve_projects("", self.projects), [])
        self.assertEqual(r.resolve_projects(None, self.projects), [])

    def test_unknown_project_raises(self):
        with self.assertRaises(SystemExit) as ctx:
            r.resolve_projects("Project A,Missing", self.projects)
        self.assertIn("找不到專案 'Missing'", str(ctx.exception))


class AssigneeAccountTests(unittest.TestCase):
    def test_normalize_account_name(self):
        self.assertEqual(r.normalize_account_name("Jane Doe"), "jane_doe")
        self.assertEqual(r.normalize_account_name(" Jane.Doe "), "jane_doe")
        self.assertEqual(r.normalize_account_name("O'Brien"), "o_brien")
        self.assertEqual(r.normalize_account_name("A  B"), "a_b")

    def test_account_aliases(self):
        self.assertEqual(
            r.account_aliases("jane_doe"),
            {"jane_doe", "janedoe", "jane.doe", "jane-doe"},
        )
        self.assertEqual(r.account_aliases(""), set())

    def test_match_by_account_alias(self):
        people = [
            {
                "id": 20,
                "name": "Alice Smith",
                "login": "",
                "account": "alice_smith",
            }
        ]
        for value in (
            "alice_smith",
            "alice.smith",
            "alice-smith",
            "alicesmith",
            "Alice Smith",
        ):
            self.assertEqual(r.match_assignee(people, value), "20")

    def test_match_is_case_insensitive(self):
        people = [{"id": 20, "name": "Alice Smith", "account": "alice_smith"}]
        self.assertEqual(r.match_assignee(people, "ALICE_SMITH"), "20")

    def test_ambiguous_match_raises(self):
        people = [
            {"id": 20, "name": "Alice Smith", "account": "alice_smith"},
            {"id": 21, "name": "Alice Stone", "account": "alice_stone"},
        ]
        with self.assertRaises(SystemExit) as ctx:
            r.match_assignee(people, "alice")
        message = str(ctx.exception)
        self.assertIn("不明確", message)
        self.assertIn("alice_smith", message)
        self.assertIn("alice_stone", message)

    def test_not_found_returns_none(self):
        people = [{"id": 20, "name": "Alice Smith", "account": "alice_smith"}]
        self.assertIsNone(r.match_assignee(people, "unknown_person"))

    def test_assignee_label(self):
        person = {"id": 20, "name": "Alice Smith", "account": "alice_smith"}
        self.assertEqual(
            r.assignee_label(person), "Alice Smith(alice_smith, 20)"
        )

    def test_me_and_numeric_pass_through(self):
        client = FakeRedmineClient([], [])
        self.assertEqual(r.resolve_assignee("me", client, "/tmp"), "me")
        self.assertEqual(r.resolve_assignee("12", client, "/tmp"), "12")
        self.assertEqual(client.calls, [])

    def test_current_user_login(self):
        client = FakeRedmineClient([], [], current_user=make_user(12, "Jane Doe", "jane_doe"))
        with tempfile.TemporaryDirectory() as output_dir:
            self.assertEqual(
                r.resolve_assignee("jane_doe", client, output_dir), "12"
            )
        self.assertEqual(
            [call[0] for call in client.calls], ["/users/current.json"]
        )

    def test_multiple_project_membership_lookup(self):
        client = FakeRedmineClient(
            [
                make_project(101, "project-a", "Project A"),
                make_project(102, "project-b", "Project B"),
            ],
            [],
            memberships={
                "project-a": [{"user": make_user(12, "Jane Doe", "jane_doe")}],
                "project-b": [
                    {"user": make_user(20, "Alice Smith", "alice_smith")}
                ],
            },
            users_forbidden=True,
        )
        with tempfile.TemporaryDirectory() as output_dir:
            self.assertEqual(
                r.resolve_assignee(
                    "alice_smith", client, output_dir, ["project-a", "project-b"]
                ),
                "20",
            )
        membership_calls = [
            call[0]
            for call in client.calls
            if call[0].endswith("/memberships.json")
        ]
        self.assertEqual(
            membership_calls,
            [
                "/projects/project-a/memberships.json",
                "/projects/project-b/memberships.json",
            ],
        )

    def test_ambiguous_account_across_projects(self):
        client = FakeRedmineClient(
            [
                make_project(101, "project-a", "Project A"),
                make_project(102, "project-b", "Project B"),
            ],
            [],
            memberships={
                "project-a": [
                    {"user": make_user(20, "Alice Smith", "alice_smith")}
                ],
                "project-b": [
                    {"user": make_user(21, "Alice Smith", "alice_smith")}
                ],
            },
            users_forbidden=True,
        )
        with tempfile.TemporaryDirectory() as output_dir:
            with self.assertRaises(SystemExit) as ctx:
                r.resolve_assignee(
                    "alice_smith", client, output_dir, ["project-a", "project-b"]
                )
        self.assertIn("不明確", str(ctx.exception))

    def test_not_found_lists_project_candidates(self):
        client = FakeRedmineClient(
            [make_project(101, "project-a", "Project A")],
            [],
            memberships={
                "project-a": [
                    {"user": make_user(20, "Alice Smith", "alice_smith")}
                ]
            },
            users_forbidden=True,
        )
        with tempfile.TemporaryDirectory() as output_dir:
            with self.assertRaises(SystemExit) as ctx:
                r.resolve_assignee(
                    "unknown_person", client, output_dir, ["project-a"]
                )
        self.assertIn("alice_smith", str(ctx.exception))


class CommandIssuesMultiProjectTests(unittest.TestCase):
    def setUp(self):
        self.project_a = make_project(101, "project-a", "Project A")
        self.project_b = make_project(102, "project-b", "Project B")
        self.project_c = make_project(103, "project-c", "Project C")
        self.bug = make_tracker(1, "Bug")
        self.feature = make_tracker(2, "Feature")
        self.new = make_status(1, "New")
        self.closed = make_status(5, "Closed", is_closed=True)
        self.issues = [
            make_issue_in_project(
                1,
                self.project_a,
                status=self.new,
                tracker=self.bug,
                updated_on="2026-01-05T00:00:00Z",
            ),
            make_issue_in_project(
                2,
                self.project_b,
                status=self.new,
                tracker=self.bug,
                updated_on="2026-01-04T00:00:00Z",
            ),
            make_issue_in_project(
                3,
                self.project_c,
                status=self.new,
                tracker=self.feature,
                updated_on="2026-01-03T00:00:00Z",
            ),
        ]
        self.client = FakeRedmineClient(
            [self.project_a, self.project_b, self.project_c],
            self.issues,
            statuses=[self.new, self.closed],
            trackers=[self.bug, self.feature],
        )

    def run_command(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            r.command_issues(args, self.client, "/tmp", 100)
        return stdout.getvalue(), stderr.getvalue()

    def issue_calls(self):
        return [
            call for call in self.client.calls if call[0] == "/issues.json"
        ]

    def test_multiple_projects_merges_all(self):
        out, _ = self.run_command(
            issues_args(project="Project A,Project B,Project C")
        )
        payload = json.loads(out)
        self.assertEqual([item["id"] for item in payload], [1, 2, 3])
        project_ids = [call[1].get("project_id") for call in self.issue_calls()]
        self.assertEqual(project_ids, ["project-a", "project-b", "project-c"])

    def test_single_project_queries_only_one(self):
        out, _ = self.run_command(issues_args(project="Project B"))
        payload = json.loads(out)
        self.assertEqual([item["id"] for item in payload], [2])
        self.assertEqual(
            [call[1].get("project_id") for call in self.issue_calls()],
            ["project-b"],
        )

    def test_no_project_omits_project_id(self):
        out, _ = self.run_command(issues_args())
        payload = json.loads(out)
        self.assertEqual([item["id"] for item in payload], [1, 2, 3])
        self.assertTrue(
            all("project_id" not in call[1] for call in self.issue_calls())
        )

    def test_limit_applied_after_merge(self):
        out, _ = self.run_command(
            issues_args(project="Project A,Project B,Project C", limit=2)
        )
        payload = json.loads(out)
        self.assertEqual([item["id"] for item in payload], [1, 2])

    def test_duplicate_issue_id_is_deduped(self):
        self.client = FakeRedmineClient(
            [self.project_a, self.project_b],
            [
                make_issue_in_project(1, self.project_a, status=self.new),
                make_issue_in_project(1, self.project_b, status=self.new),
            ],
            statuses=[self.new, self.closed],
            trackers=[self.bug, self.feature],
        )
        out, _ = self.run_command(issues_args(project="Project A,Project B"))
        payload = json.loads(out)
        self.assertEqual([item["id"] for item in payload], [1])

    def test_status_all_and_tracker_filter(self):
        out, _ = self.run_command(
            issues_args(
                project="Project A,Project B,Project C",
                status="all",
                tracker="Bug",
            )
        )
        payload = json.loads(out)
        self.assertEqual([item["id"] for item in payload], [1, 2])

    def test_invalid_project_in_list_raises(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_command(issues_args(project="Project A,Missing"))
        self.assertIn("找不到專案 'Missing'", str(ctx.exception))


class CommandStatsMultiProjectTests(unittest.TestCase):
    def setUp(self):
        self.project_a = make_project(101, "project-a", "Project A")
        self.project_b = make_project(102, "project-b", "Project B")
        self.project_c = make_project(103, "project-c", "Project C")
        self.new = make_status(1, "New")
        self.resolved = make_status(3, "Resolved")
        self.closed = make_status(5, "Closed", is_closed=True)
        self.issues = [
            make_issue_in_project(1, self.project_a, status=self.new),
            make_issue_in_project(2, self.project_a, status=self.resolved),
            make_issue_in_project(3, self.project_a, status=self.closed),
            make_issue_in_project(4, self.project_b, status=self.new),
            make_issue_in_project(5, self.project_b, status=self.resolved),
            make_issue_in_project(6, self.project_c, status=self.new),
        ]
        self.client = FakeRedmineClient(
            [self.project_a, self.project_b, self.project_c],
            self.issues,
            statuses=[self.new, self.resolved, self.closed],
        )

    def run_command(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            r.command_stats(args, self.client, "/tmp", 100)
        return stdout.getvalue(), stderr.getvalue()

    def test_stats_multiple_projects_json(self):
        out, _ = self.run_command(
            stats_args(project="Project A,Project B,Project C")
        )
        records = {item["project"]: item for item in json.loads(out)}
        self.assertEqual(records["Project A"]["open"], 1)
        self.assertEqual(records["Project A"]["resolved"], 1)
        self.assertEqual(records["Project A"]["closed"], 1)
        self.assertEqual(records["Project A"]["total"], 3)
        self.assertEqual(records["Project B"]["open"], 1)
        self.assertEqual(records["Project B"]["resolved"], 1)
        self.assertEqual(records["Project B"]["closed"], 0)
        self.assertEqual(records["Project B"]["total"], 2)
        self.assertEqual(records["Project C"]["open"], 1)
        self.assertEqual(records["Project C"]["resolved"], 0)
        self.assertEqual(records["Project C"]["closed"], 0)
        self.assertEqual(records["Project C"]["total"], 1)

    def test_stats_single_project_compatibility(self):
        out, _ = self.run_command(stats_args(project="Project A"))
        records = json.loads(out)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["project"], "Project A")
        self.assertEqual(records[0]["total"], 3)

    def test_stats_by_resolution_multiple_projects(self):
        self.issues[0]["journals"] = [
            {
                "user": {"id": 12},
                "details": [
                    {"property": "attr", "name": "status_id", "new_value": "3"}
                ],
            }
        ]
        self.issues[4]["journals"] = [
            {
                "user": {"id": 99},
                "details": [
                    {"property": "attr", "name": "status_id", "new_value": "3"}
                ],
            }
        ]
        self.issues[5]["journals"] = [
            {
                "user": {"id": 12},
                "details": [
                    {"property": "attr", "name": "status_id", "new_value": "3"}
                ],
            }
        ]
        out, _ = self.run_command(
            stats_args(
                project="Project A,Project B,Project C", by_resolution=True
            )
        )
        records = {item["project"]: item["resolved"] for item in json.loads(out)}
        self.assertEqual(records, {"Project A": 1, "Project C": 1})


class CommandExportMultiProjectTests(unittest.TestCase):
    def setUp(self):
        self.project_a = make_project(101, "project-a", "Project A")
        self.project_b = make_project(102, "project-b", "Project B")
        self.project_c = make_project(103, "project-c", "Project C")
        self.issues = [
            make_issue_in_project(1, self.project_a),
            make_issue_in_project(2, self.project_b),
            make_issue_in_project(3, self.project_c),
        ]
        self.client = FakeRedmineClient(
            [self.project_a, self.project_b, self.project_c], self.issues
        )

    def run_command(self, args, script_dir="/tmp"):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            r.command_export(args, self.client, script_dir)
        return stdout.getvalue(), stderr.getvalue()

    def test_export_json_merges_multiple_projects(self):
        with tempfile.TemporaryDirectory() as output_dir:
            out, _ = self.run_command(
                export_args(
                    project="Project A,Project B,Project C",
                    output_dir=output_dir,
                )
            )
            data = json.loads(
                (Path(output_dir) / "issues.json").read_text(encoding="utf-8")
            )
        self.assertEqual([item["id"] for item in data], [1, 2, 3])
        self.assertIn("已寫入", out)

    def test_export_dry_run_multiple_projects(self):
        with tempfile.TemporaryDirectory() as output_dir:
            out, _ = self.run_command(
                export_args(
                    project="Project A,Project B,Project C",
                    output_dir=output_dir,
                    dry_run=True,
                )
            )
        self.assertIn("專案 project-a 共 1 筆", out)
        self.assertIn("專案 project-b 共 1 筆", out)
        self.assertIn("專案 project-c 共 1 筆", out)

    def test_export_project_name_is_resolved_to_identifier(self):
        with tempfile.TemporaryDirectory() as output_dir:
            self.run_command(
                export_args(project="Project A", output_dir=output_dir)
            )
        project_ids = [
            call[1].get("project_id")
            for call in self.client.calls
            if call[0] == "/issues.json"
        ]
        self.assertEqual(project_ids, ["project-a"])

    def test_export_dedupes_duplicate_issue_ids(self):
        duplicate = make_issue_in_project(1, self.project_b)
        self.client.issues = self.issues + [duplicate]
        with tempfile.TemporaryDirectory() as output_dir:
            self.run_command(
                export_args(
                    project="Project A,Project B", output_dir=output_dir
                )
            )
            data = json.loads(
                (Path(output_dir) / "issues.json").read_text(encoding="utf-8")
            )
        self.assertEqual([item["id"] for item in data], [1, 2])

    def test_export_invalid_project_raises(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_command(export_args(project="Missing"))
        self.assertIn("找不到專案 'Missing'", str(ctx.exception))


class ParserMultiProjectTests(unittest.TestCase):
    def test_issues_parser_accepts_comma_separated_projects(self):
        parser = r.build_parser()
        args, unknown = parser.parse_known_args(
            ["issues", "--project", "Project A,Project B"]
        )
        self.assertEqual(unknown, [])
        self.assertEqual(args.project, "Project A,Project B")

    def test_stats_parser_accepts_comma_separated_projects(self):
        parser = r.build_parser()
        args, unknown = parser.parse_known_args(
            ["stats", "--project", "Project A,Project B"]
        )
        self.assertEqual(unknown, [])
        self.assertEqual(args.project, "Project A,Project B")

    def test_export_parser_accepts_comma_separated_projects(self):
        parser = r.build_parser()
        args, unknown = parser.parse_known_args(
            ["export", "--project", "Project A,Project B"]
        )
        self.assertEqual(unknown, [])
        self.assertEqual(args.project, "Project A,Project B")


class AssigneeCacheTests(unittest.TestCase):
    def write_cache(self, output_dir, people, base_url="https://redmine.example"):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            r.write_assignee_cache(output_dir, base_url, people)
        return stderr.getvalue()

    def test_write_and_load_roundtrip(self):
        people = [
            {
                "id": 20,
                "name": "Alice Smith",
                "login": "alice_smith",
                "account": "alice_smith",
                "total": 3,
                "open": 2,
            }
        ]
        with tempfile.TemporaryDirectory() as output_dir:
            self.write_cache(output_dir, people)
            loaded = r.load_assignee_cache(
                output_dir, "https://redmine.example"
            )
        self.assertEqual(loaded, people)

    def test_load_wrong_base_url_returns_none(self):
        with tempfile.TemporaryDirectory() as output_dir:
            self.write_cache(
                output_dir,
                [
                    {
                        "id": 20,
                        "name": "Alice Smith",
                        "account": "alice_smith",
                        "total": 0,
                        "open": 0,
                    }
                ],
            )
            loaded = r.load_assignee_cache(
                output_dir, "https://other.example"
            )
        self.assertIsNone(loaded)

    def test_load_malformed_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as output_dir:
            path = Path(output_dir) / "assignees.json"
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(
                r.load_assignee_cache(output_dir, "https://redmine.example")
            )

    def test_write_dedupes_by_id(self):
        people = [
            {
                "id": 20,
                "name": "Alice Smith",
                "account": "alice_smith",
                "total": 1,
                "open": 1,
            },
            {
                "id": 20,
                "name": "Alice S.",
                "account": "alice_s",
                "total": 2,
                "open": 0,
            },
        ]
        with tempfile.TemporaryDirectory() as output_dir:
            self.write_cache(output_dir, people)
            loaded = r.load_assignee_cache(
                output_dir, "https://redmine.example"
            )
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "Alice S.")
        self.assertEqual(loaded[0]["total"], 2)

    def test_resolve_uses_cache_without_client_calls(self):
        people = [
            {
                "id": 20,
                "name": "Alice Smith",
                "account": "alice_smith",
                "total": 0,
                "open": 0,
            }
        ]
        client = FakeRedmineClient([], [], users_forbidden=True)
        with tempfile.TemporaryDirectory() as output_dir:
            self.write_cache(output_dir, people)
            self.assertEqual(
                r.resolve_assignee("alice_smith", client, output_dir), "20"
            )
        self.assertEqual(client.calls, [])

    def test_resolve_uses_export_file(self):
        project = make_project(101, "project-a", "Project A")
        issue = make_issue_in_project(
            1,
            project,
            assignee=make_user(20, "Alice Smith", ""),
        )
        client = FakeRedmineClient([], [], users_forbidden=True)
        with tempfile.TemporaryDirectory() as output_dir:
            (Path(output_dir) / "issues.json").write_text(
                json.dumps([issue]), encoding="utf-8"
            )
            self.assertEqual(
                r.resolve_assignee("alice_smith", client, output_dir), "20"
            )
            self.assertTrue((Path(output_dir) / "assignees.json").exists())
        self.assertEqual(client.calls, [])

    def test_admin_user_lookup(self):
        client = FakeRedmineClient(
            [],
            [],
            current_user=make_user(12, "Jane Doe", "jane_doe"),
            users=[make_user(20, "Alice Smith", "alice_smith")],
        )
        with tempfile.TemporaryDirectory() as output_dir:
            self.assertEqual(
                r.resolve_assignee("alice_smith", client, output_dir), "20"
            )
        self.assertIn("/users.json", [call[0] for call in client.calls])

    def test_users_api_forbidden_raises_with_instructions(self):
        client = FakeRedmineClient(
            [],
            [],
            current_user=make_user(12, "Jane Doe", "jane_doe"),
            users_forbidden=True,
        )
        with tempfile.TemporaryDirectory() as output_dir:
            with self.assertRaises(SystemExit) as ctx:
                r.resolve_assignee("alice_smith", client, output_dir)
        self.assertIn("assignees --live", str(ctx.exception))


class PeopleCollectionTests(unittest.TestCase):
    def test_collect_people_counts_open_and_closed(self):
        project = make_project(101, "project-a", "Project A")
        open_status = make_status(1, "New")
        closed_status = make_status(5, "Closed", is_closed=True)
        issues = [
            make_issue_in_project(
                1,
                project,
                status=open_status,
                assignee=make_user(20, "Alice Smith", ""),
            ),
            make_issue_in_project(
                2,
                project,
                status=closed_status,
                assignee=make_user(20, "Alice Smith", ""),
            ),
        ]
        people = r.collect_people(issues, {5})
        self.assertEqual(
            people,
            [
                {
                    "id": 20,
                    "name": "Alice Smith",
                    "login": "",
                    "account": "alice_smith",
                    "total": 2,
                    "open": 1,
                }
            ],
        )

    def test_fetch_projects_people_dedupes_overlapping_users(self):
        client = FakeRedmineClient(
            [
                make_project(101, "project-a", "Project A"),
                make_project(102, "project-b", "Project B"),
            ],
            [],
            memberships={
                "project-a": [
                    {"user": make_user(20, "Alice Smith", "alice_smith")},
                    {"user": make_user(21, "Bob Jones", "bob_jones")},
                ],
                "project-b": [
                    {"user": make_user(20, "Alice Smith", "alice_smith")}
                ],
            },
        )
        people = r.fetch_projects_people(client, ["project-a", "project-b"])
        self.assertEqual([person["id"] for person in people], [20, 21])


class CommandIssuesOutputTests(unittest.TestCase):
    def setUp(self):
        self.project_a = make_project(101, "project-a", "Project A")
        self.project_b = make_project(102, "project-b", "Project B")
        self.project_c = make_project(103, "project-c", "Project C")
        self.new = make_status(1, "New")
        self.closed = make_status(5, "Closed", is_closed=True)
        self.bug = make_tracker(1, "Bug")
        self.issues = [
            make_issue_in_project(1, self.project_a, status=self.new),
            make_issue_in_project(2, self.project_b, status=self.new),
            make_issue_in_project(3, self.project_c, status=self.new),
        ]
        self.client = FakeRedmineClient(
            [self.project_a, self.project_b, self.project_c],
            self.issues,
            statuses=[self.new, self.closed],
            trackers=[self.bug],
        )

    def run_command(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            r.command_issues(args, self.client, "/tmp", 100)
        return stdout.getvalue(), stderr.getvalue()

    def test_table_output(self):
        out, _ = self.run_command(
            issues_args(project="Project A,Project B,Project C", format="table")
        )
        self.assertIn("Project A", out)
        self.assertIn("Project B", out)
        self.assertIn("Project C", out)

    def test_csv_output(self):
        out, _ = self.run_command(
            issues_args(project="Project A,Project B,Project C", format="csv")
        )
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(
            rows[0],
            [
                "id",
                "project",
                "tracker",
                "status",
                "subject",
                "url",
                "assigned_to",
            ],
        )
        self.assertEqual([row[0] for row in rows[1:]], ["1", "2", "3"])

    def test_no_records_message(self):
        self.client.issues = []
        out, _ = self.run_command(issues_args(format="table"))
        self.assertIn("沒有符合條件的 issue", out)

    def test_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "issues.json"
            out, _ = self.run_command(
                issues_args(
                    project="Project A,Project B,Project C",
                    format="json",
                    output=str(path),
                )
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(out, "")
        self.assertEqual([item["id"] for item in payload], [1, 2, 3])

    def test_account_resolution_across_projects(self):
        alice = make_user(20, "Alice Smith", "alice_smith")
        self.issues[0]["assigned_to"] = make_user(12, "Jane Doe", "jane_doe")
        self.issues[1]["assigned_to"] = alice
        self.client = FakeRedmineClient(
            [self.project_a, self.project_b],
            self.issues[:2],
            statuses=[self.new, self.closed],
            trackers=[self.bug],
            memberships={
                "project-a": [{"user": make_user(12, "Jane Doe", "jane_doe")}],
                "project-b": [{"user": alice}],
            },
            users_forbidden=True,
        )
        out, _ = self.run_command(
            issues_args(
                project="Project A,Project B",
                assignee="alice_smith",
            )
        )
        payload = json.loads(out)
        self.assertEqual([item["id"] for item in payload], [2])


class CommandStatsOutputTests(unittest.TestCase):
    def setUp(self):
        self.project_a = make_project(101, "project-a", "Project A")
        self.project_b = make_project(102, "project-b", "Project B")
        self.new = make_status(1, "New")
        self.resolved = make_status(3, "Resolved")
        self.closed = make_status(5, "Closed", is_closed=True)
        self.issues = [
            make_issue_in_project(1, self.project_a, status=self.new),
            make_issue_in_project(2, self.project_a, status=self.resolved),
            make_issue_in_project(3, self.project_b, status=self.closed),
        ]
        self.client = FakeRedmineClient(
            [self.project_a, self.project_b],
            self.issues,
            statuses=[self.new, self.resolved, self.closed],
        )

    def run_command(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            r.command_stats(args, self.client, "/tmp", 100)
        return stdout.getvalue(), stderr.getvalue()

    def test_csv_output(self):
        out, _ = self.run_command(
            stats_args(project="Project A,Project B", format="csv")
        )
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(
            rows[0],
            ["project_id", "project", "open", "resolved", "closed", "total"],
        )
        self.assertEqual([row[1] for row in rows[1:]], ["Project A", "Project B"])

    def test_no_records_message(self):
        self.client.issues = []
        out, _ = self.run_command(stats_args(format="table"))
        self.assertIn("沒有符合條件的 issue", out)

    def test_by_resolution_no_records_message(self):
        self.client.issues = []
        out, _ = self.run_command(
            stats_args(by_resolution=True, format="table")
        )
        self.assertIn("沒有符合條件的 issue", out)


class CommandExportOutputTests(unittest.TestCase):
    def setUp(self):
        self.project_a = make_project(101, "project-a", "Project A")
        self.project_b = make_project(102, "project-b", "Project B")
        self.issues = [
            make_issue_in_project(1, self.project_a),
            make_issue_in_project(2, self.project_b),
        ]
        self.client = FakeRedmineClient(
            [self.project_a, self.project_b], self.issues
        )

    def run_command(self, args, script_dir="/tmp"):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            r.command_export(args, self.client, script_dir)
        return stdout.getvalue(), stderr.getvalue()

    def test_csv_output(self):
        with tempfile.TemporaryDirectory() as output_dir:
            self.run_command(
                export_args(
                    project="Project A,Project B",
                    format="csv",
                    output_dir=output_dir,
                )
            )
            text = (Path(output_dir) / "issues.csv").read_text(
                encoding="utf-8-sig"
            )
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0][0], "id")
        self.assertEqual([row[0] for row in rows[1:]], ["1", "2"])

    def test_both_output(self):
        with tempfile.TemporaryDirectory() as output_dir:
            self.run_command(
                export_args(
                    project="Project A,Project B",
                    format="both",
                    output_dir=output_dir,
                )
            )
            self.assertTrue((Path(output_dir) / "issues.json").exists())
            self.assertTrue((Path(output_dir) / "issues.csv").exists())

    def test_dry_run_no_project(self):
        with tempfile.TemporaryDirectory() as output_dir:
            out, _ = self.run_command(
                export_args(output_dir=output_dir, dry_run=True)
            )
        self.assertIn("全部專案 共 2 筆 issue", out)

    def test_dry_run_single_project_uses_original_label(self):
        with tempfile.TemporaryDirectory() as output_dir:
            out, _ = self.run_command(
                export_args(
                    project="Project A", output_dir=output_dir, dry_run=True
                )
            )
        self.assertIn("專案 Project A 共 1 筆 issue", out)


class ParserDefaultsTests(unittest.TestCase):
    def test_issues_defaults(self):
        parser = r.build_parser()
        args, _ = parser.parse_known_args(["issues"])
        self.assertEqual(args.assignee, "me")
        self.assertEqual(args.status, "open")
        self.assertIsNone(args.project)
        self.assertEqual(args.format, "table")
        self.assertEqual(args.limit, 0)
        self.assertEqual(args.sort, "updated_on:desc")

    def test_stats_defaults(self):
        parser = r.build_parser()
        args, _ = parser.parse_known_args(["stats"])
        self.assertEqual(args.assignee, "me")
        self.assertIsNone(args.project)
        self.assertEqual(args.resolved_status, "Resolved")
        self.assertFalse(args.by_resolution)
        self.assertEqual(args.format, "table")

    def test_export_defaults(self):
        parser = r.build_parser()
        args, _ = parser.parse_known_args(["export"])
        self.assertIsNone(args.project)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.format, "both")
        self.assertIsNone(args.output_dir)
        self.assertEqual(args.page_size, 100)
        self.assertEqual(args.csv_description_limit, 32000)


class CommandIssueOutputFileTests(unittest.TestCase):
    def test_json_output_file(self):
        client = FakeClient({1: make_issue(1)})
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "issue.json"
            args = make_args(format="json", output=str(path))
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                r.command_issue(args, client)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(payload["id"], 1)


if __name__ == "__main__":
    unittest.main()
