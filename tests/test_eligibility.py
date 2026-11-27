import json
import subprocess
import unittest
from unittest.mock import patch

from factory.eligibility import read_candidate


REPO = "modelscope/evalscope"
ISSUE = f"https://github.com/{REPO}/issues/2225"
TASK = {"upstream": ISSUE, "title": f"{REPO} #2225 · GraphWalks"}
STAMP = "2026-11-27T00:00:00Z"


def connection(nodes, flag="hasNextPage"):
    return {"totalCount": len(nodes), "nodes": nodes, "pageInfo": {flag: False}}


def search_result(items):
    return {"incomplete_results": False, "total_count": len(items), "items": items}


def issue_comment(excerpt):
    return {"id": "comment-id", "url": f"{ISSUE}#issuecomment-100", "author": {"login": "contributor"},
            "bodyText": excerpt, "createdAt": STAMP, "updatedAt": STAMP}


def issue_fixture():
    return {
        "id": "issue-id", "number": 2225, "url": ISSUE, "state": "OPEN",
        "title": "Track benchmark integration", "updatedAt": STAMP,
        "bodyText": "Integrate separate benchmarks. Order 1 is Global-MMLU. Order 2 is GraphWalks.",
        "assignees": connection([]),
        "comments": connection([issue_comment("I'd like to take Global-MMLU (order 1).")]),
        "closedByPullRequestsReferences": connection([]),
        "timelineItems": connection([], "hasPreviousPage"),
    }


def pull(number, title, body=None):
    return {"html_url": f"https://github.com/{REPO}/pull/{number}", "state": "open", "title": title,
            "updated_at": STAMP, "pull_request": {}, "body": body}


def graphql_pull(number, title, state="OPEN"):
    return {"__typename": "PullRequest", "url": f"https://github.com/{REPO}/pull/{number}", "state": state,
            "title": title, "updatedAt": STAMP, "repository": {"nameWithOwner": REPO, "isPrivate": False}}


class FakeGitHub:
    def __init__(self):
        self.issue = issue_fixture()
        self.is_private = False
        self.results = {"2225": search_result([]), '"GraphWalks"': search_result([])}
        self.searches = []

    def __call__(self, *args):
        if args[0] == "graphql":
            return {"data": {"repository": {"nameWithOwner": REPO, "isPrivate": self.is_private,
                                            "issue": self.issue}}}
        expected = f"q=repo:{REPO} is:pr is:open in:title,body "
        self.searches.append(args[4])
        return self.results.get(args[4].removeprefix(expected), search_result([]))


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.enterContext(patch("factory.eligibility._api", self.github))

    def test_scope_comment_is_available_for_native_judgment(self):
        result = read_candidate(TASK)
        self.assertFalse(result["blocked"])
        self.assertRegex(result["revision"], "[a-f0-9]{64}")
        context = result["context"]
        self.assertEqual(context["scope"], "GraphWalks")
        self.assertEqual(context["assignees"], [])
        self.assertTrue(context["requires_scope_review"])
        self.assertEqual(context["recent_comments"][0]["excerpt"], "I'd like to take Global-MMLU (order 1).")
        self.assertTrue(any('"GraphWalks"' in query for query in self.github.searches))

    def test_assignee_close_and_pr_are_independent_blockers(self):
        self.github.issue["state"] = "CLOSED"
        self.github.issue["assignees"] = connection([{"login": "maintainer"}])
        self.github.issue["closedByPullRequestsReferences"] = connection([graphql_pull(2300, "Claimed work")])
        result = read_candidate(TASK)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reasons"], ["issue_closed", "issue_assigned", "competing_open_pr"])
        self.assertEqual(result["context"]["open_prs"][0]["relationships"], ["linked_to_issue"])

    def test_search_blocks_explicit_overlap_and_keeps_other_items_as_context(self):
        self.github.results["2225"] = search_result([pull(2300, "Add Global-MMLU", body="First item in #2225")])
        other = read_candidate(TASK)
        self.assertFalse(other["blocked"])
        self.assertFalse(other["context"]["open_prs"][0]["competing"])
        self.assertEqual(other["context"]["open_prs"][0]["excerpt"], "First item in #2225")
        self.github.results['"GraphWalks"'] = search_result([pull(2301, "feat: GraphWalks adapter")])
        matching = read_candidate(TASK)
        self.assertEqual(matching["reasons"], ["competing_open_pr"])
        self.assertNotEqual(other["revision"], matching["revision"])
        self.github.results['"GraphWalks"'] = search_result([])
        self.github.results["2225"] = search_result([pull(2302, "Implement an unnamed adapter", f"Fixes {ISSUE}.")])
        self.assertTrue(read_candidate(TASK)["blocked"])
        self.github.results["2225"] = search_result([pull(2303, "Implement an unnamed adapter", "Fixes fork/repo#2225")])
        self.assertFalse(read_candidate(TASK)["blocked"])

    def test_timeline_removes_disconnected_open_pr_and_retains_unclear_reference(self):
        pr = graphql_pull(2300, "Other work")
        issue = {"__typename": "Issue"}
        cross_reference = {"__typename": "CrossReferencedEvent", "source": pr, "willCloseTarget": False}
        connected = {"__typename": "ConnectedEvent", "source": issue, "subject": pr}
        disconnected = {"__typename": "DisconnectedEvent", "source": issue, "subject": pr}
        self.github.issue["timelineItems"] = connection([cross_reference, connected], "hasPreviousPage")
        self.assertTrue(read_candidate(TASK)["blocked"])
        self.github.issue["timelineItems"] = connection([cross_reference, connected, disconnected], "hasPreviousPage")
        result = read_candidate(TASK)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["context"]["open_prs"][0]["relationships"], ["cross_reference"])
        cross_reference["willCloseTarget"] = True
        self.assertEqual(read_candidate(TASK)["reasons"], ["competing_open_pr"])

    def test_missing_and_incomplete_evidence_does_not_mean_unclaimed(self):
        incomplete_responses = (
            {"incomplete_results": True, "total_count": 0, "items": []},
            {"incomplete_results": False, "total_count": 32, "items": []},
            {"total_count": 0, "items": []},
        )
        for response in incomplete_responses:
            self.github.results["2225"] = response
            result = read_candidate(TASK)
            self.assertTrue(result["blocked"])
            self.assertIsNone(result["revision"])
            self.assertEqual(result["context"]["completeness"], "unknown")
        self.github.results["2225"] = search_result([])
        self.github.issue["assignees"]["pageInfo"]["hasNextPage"] = True
        self.assertEqual(read_candidate(TASK)["reasons"], ["github_incomplete_connection"])

    def test_revision_is_order_stable_and_tracks_comment_pr_and_assignee_changes(self):
        first, second = pull(2300, "Maybe first", "See #2225"), pull(2301, "Maybe second")
        self.github.results["2225"] = search_result([first, second])
        base = read_candidate(TASK)["revision"]
        self.github.results["2225"] = search_result([second, first])
        self.assertEqual(base, read_candidate(TASK)["revision"])
        self.github.issue["comments"]["nodes"][0]["bodyText"] += " Edit."
        after_comment = read_candidate(TASK)["revision"]
        self.assertNotEqual(base, after_comment)
        self.github.issue["assignees"] = connection([{"login": "contributor"}])
        after_assignee = read_candidate(TASK)["revision"]
        self.assertNotEqual(after_comment, after_assignee)
        first["body"] += " Additional context."
        self.assertNotEqual(after_assignee, read_candidate(TASK)["revision"])

    def test_private_scope_and_private_repositories_are_not_disclosed(self):
        private = {**TASK, "title": "An internal title with a credential"}
        result = read_candidate(private)
        self.assertFalse(result["blocked"])
        self.assertNotIn("credential", json.dumps(result))
        self.assertNotIn("credential", json.dumps(self.github.searches))
        self.github.is_private = True
        self.assertEqual(read_candidate(TASK)["context"], {"completeness": "unknown"})


class GitHubTransportTests(unittest.TestCase):
    def test_unsuccessful_cli_or_graphql_response_fails_closed(self):
        replies = (
            (subprocess.CompletedProcess([], 1, "", "HTTP 403 rate limit private-diagnostic"), "github_rate_limited"),
            (subprocess.CompletedProcess([], 0, json.dumps({"data": None, "errors": [{"message": "private-diagnostic"}]}), ""),
             "github_invalid_response"),
            (subprocess.CompletedProcess([], 0, "private-diagnostic", ""), "github_invalid_response"),
        )
        for reply, reason in replies:
            with self.subTest(exit_code=reply.returncode), patch("factory.eligibility.subprocess.run", return_value=reply):
                result = read_candidate(TASK)
                self.assertTrue(result["blocked"])
                self.assertIsNone(result["revision"])
                self.assertEqual(result["reasons"], [reason])
                self.assertNotIn("private-diagnostic", json.dumps(result))
