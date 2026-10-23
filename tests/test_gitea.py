import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gitea_ocr_review.errors import GiteaError
from gitea_ocr_review.gitea import GiteaClient


class _State:
    def __init__(self):
        self.requests = []
        self.responses = {}


class _Handler(BaseHTTPRequestHandler):
    state: _State

    def log_message(self, *_args):  # silence test output
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else None
        key = (self.command, self.path)
        self.state.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body) if body else None,
            }
        )
        status, payload = self.state.responses.get(key, (200, {}))
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


class GiteaClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = _State()
        _Handler.state = cls.state
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.state.requests.clear()
        self.state.responses.clear()

    def test_base_url_normalisation_and_auth(self):
        self.state.responses[("GET", "/api/v1/repos/o/r/pulls/3")] = (
            200,
            {"number": 3, "title": "T"},
        )
        client = GiteaClient(self.base + "/api/v1", "secret", no_proxy=True)
        pull = client.get_pull("o", "r", 3)
        self.assertEqual(pull["number"], 3)
        request = self.state.requests[-1]
        self.assertEqual(request["authorization"], "token secret")

    def test_create_review_payload(self):
        self.state.responses[("POST", "/api/v1/repos/o/r/pulls/1/reviews")] = (
            200,
            {"id": 7, "state": "REQUEST_CHANGES"},
        )
        client = GiteaClient(self.base, "secret", no_proxy=True)
        review = client.create_review(
            "o",
            "r",
            1,
            event="REQUEST_CHANGES",
            body="body",
            comments=[{"path": "a.py", "new_position": 2, "old_position": 0, "body": "x"}],
            commit_id="deadbeef",
        )
        self.assertEqual(review["id"], 7)
        body = self.state.requests[-1]["body"]
        self.assertEqual(body["event"], "REQUEST_CHANGES")
        self.assertEqual(body["commit_id"], "deadbeef")
        self.assertEqual(body["comments"][0]["new_position"], 2)

    def test_error_message_is_surfaced(self):
        self.state.responses[("POST", "/api/v1/repos/o/r/pulls/1/reviews")] = (
            422,
            {"message": "approve your own pull is not allowed"},
        )
        client = GiteaClient(self.base, "secret", no_proxy=True)
        with self.assertRaises(GiteaError) as ctx:
            client.create_review("o", "r", 1, event="APPROVED", body="x")
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("approve your own pull", str(ctx.exception))

    def test_unreachable_server(self):
        client = GiteaClient("http://127.0.0.1:1", "secret", no_proxy=True, timeout=1)
        with self.assertRaises(GiteaError):
            client.get_authenticated_user()

    def test_list_pulls_query_string(self):
        self.state.responses[("GET", "/api/v1/repos/o/r/pulls?state=open&limit=5&page=1")] = (
            200,
            [{"number": 1}],
        )
        client = GiteaClient(self.base, "secret", no_proxy=True)
        pulls = client.list_pulls("o", "r", state="open", limit=5)
        self.assertEqual(pulls[0]["number"], 1)

    def test_list_reviews_follows_pagination(self):
        # A pending REQUEST_REVIEW row is the newest, so it can land on the
        # second page; list_reviews must not stop after page one.
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews?limit=2&page=1")
        ] = (200, [{"id": 1, "state": "APPROVED"}, {"id": 2, "state": "APPROVED"}])
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews?limit=2&page=2")
        ] = (200, [{"id": 3, "state": "REQUEST_REVIEW", "team": {"id": 7}}])
        client = GiteaClient(self.base, "secret", no_proxy=True)
        reviews = client.list_reviews("o", "r", 1, limit=2)
        self.assertEqual([r["id"] for r in reviews], [1, 2, 3])
        self.assertEqual(reviews[-1]["state"], "REQUEST_REVIEW")

    def test_list_issue_comments(self):
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/issues/1/comments?limit=50&page=1")
        ] = (200, [{"id": 1, "body": "hello"}])
        client = GiteaClient(self.base, "secret", no_proxy=True)
        comments = client.list_issue_comments("o", "r", 1)
        self.assertEqual(comments[0]["body"], "hello")

    def test_list_review_comments(self):
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/comments?limit=50&page=1")
        ] = (200, [{"id": 2, "body": "line note", "path": "a.py", "line": 3}])
        client = GiteaClient(self.base, "secret", no_proxy=True)
        comments = client.list_review_comments("o", "r", 1)
        self.assertEqual(comments[0]["path"], "a.py")

    def test_list_review_comments_falls_back_to_reviews(self):
        # Gitea returns 404 for /pulls/{n}/comments and only serves line
        # comments per review, so the client must walk the reviews.
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/comments?limit=50&page=1")
        ] = (404, {"message": "not found"})
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews?limit=50&page=1")
        ] = (200, [{"id": 11}, {"id": 12}])
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews/11/comments?limit=50&page=1")
        ] = (200, [{"id": 2, "body": "line note", "path": "a.py"}])
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews/12/comments?limit=50&page=1")
        ] = (200, [])
        client = GiteaClient(self.base, "secret", no_proxy=True)
        comments = client.list_review_comments("o", "r", 1)
        self.assertEqual([c["id"] for c in comments], [2])
        paths = [r["path"] for r in self.state.requests]
        self.assertIn(
            "/api/v1/repos/o/r/pulls/1/reviews/11/comments?limit=50&page=1", paths
        )

    def test_comment_lists_follow_pagination(self):
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/issues/1/comments?limit=2&page=1")
        ] = (200, [{"id": 1}, {"id": 2}])
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/issues/1/comments?limit=2&page=2")
        ] = (200, [{"id": 3}])
        client = GiteaClient(self.base, "secret", no_proxy=True)
        comments = client.list_issue_comments("o", "r", 1, limit=2)
        self.assertEqual([c["id"] for c in comments], [1, 2, 3])

    def test_list_reviews_stops_after_a_short_page(self):
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews?limit=50&page=1")
        ] = (200, [{"id": 1, "state": "APPROVED"}])
        client = GiteaClient(self.base, "secret", no_proxy=True)
        self.assertEqual(len(client.list_reviews("o", "r", 1)), 1)
        paths = [r["path"] for r in self.state.requests]
        self.assertEqual(paths, ["/api/v1/repos/o/r/pulls/1/reviews?limit=50&page=1"])


if __name__ == "__main__":
    unittest.main()
