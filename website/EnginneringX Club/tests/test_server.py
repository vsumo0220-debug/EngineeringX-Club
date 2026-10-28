import http.cookiejar
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

from PIL import Image


SITE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SITE_DIR))

from server import EngineeringXApp, make_handler  # noqa: E402


image_buffer = BytesIO()
Image.new("RGB", (2, 2), "forestgreen").save(image_buffer, format="JPEG")
JPEG = image_buffer.getvalue()
REQUIRED_FIELDS = {
    "title": "Sample project",
    "creator_display_name": "A student",
    "description": "It measures and logs system power use.",
    "category": "Electronics & Embedded Systems",
}


def encoded_image(image_format):
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "forestgreen").save(buffer, format=image_format)
    return buffer.getvalue()


def multipart(fields, include_image=True, image=JPEG):
    boundary = f"----EngineeringX{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    if include_image:
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="image"; filename="actual-project.jpg"\r\n',
                b"Content-Type: image/jpeg\r\n\r\n",
                image,
                b"\r\n",
            ]
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class ProjectSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "persistent-data"
        self.app = EngineeringXApp(self.data_dir, SITE_DIR, "a-long-test-admin-password")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.cookies = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp_dir.cleanup()

    def request(self, path, method="GET", body=None, headers=None, client=None):
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            response = (client or self.client).open(request)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers
        return response.status, response.read(), response.headers

    def submit(self, fields=None, include_image=True):
        body, content_type = multipart(fields or REQUIRED_FIELDS, include_image)
        return self.request(
            "/api/projects",
            "POST",
            body,
            {"Content-Type": content_type},
        )

    def sign_in(self):
        status, _, _ = self.request(
            "/api/admin/login",
            "POST",
            json.dumps({"password": "a-long-test-admin-password"}).encode(),
            {"Content-Type": "application/json", "X-Admin-Action": "1"},
        )
        self.assertEqual(status, 200)

    def test_requires_image_and_description(self):
        status, body, _ = self.submit(include_image=False)
        self.assertEqual(status, 400)
        self.assertIn(b"image", body)

        fields = {**REQUIRED_FIELDS, "description": ""}
        status, body, _ = self.submit(fields)
        self.assertEqual(status, 400)
        self.assertIn(b"Description is required", body)

    def test_optional_platform_links_and_invalid_urls(self):
        cases = [
            {},
            {"github_url": "https://github.com/student/project"},
            {"youtube_url": "https://youtu.be/example"},
            {
                "github_url": "https://github.com/student/project",
                "youtube_url": "https://www.youtube.com/watch?v=example",
            },
        ]
        for links in cases:
            with self.subTest(links=links):
                status, body, _ = self.submit({**REQUIRED_FIELDS, **links})
                self.assertEqual(status, 201, body)

        for field, url in (
            ("github_url", "https://github.com.attacker.invalid/repo"),
            ("github_url", "javascript://github.com/repo"),
            ("youtube_url", "https://video.invalid/watch/123"),
        ):
            with self.subTest(field=field, url=url):
                status, _, _ = self.submit({**REQUIRED_FIELDS, field: url})
                self.assertEqual(status, 400)

    def test_pending_projects_are_private_until_approved_and_image_persists(self):
        status, body, _ = self.submit()
        self.assertEqual(status, 201)
        project_id = json.loads(body)["id"]
        status, body, _ = self.request("/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

        anonymous = urllib.request.build_opener()
        status, _, _ = self.request(f"/uploads/{self.app.admin_projects('pending')[0]['cover_image_path']}", client=anonymous)
        self.assertEqual(status, 404)
        status, _, _ = self.request(f"/api/admin/projects/{project_id}/image", client=anonymous)
        self.assertEqual(status, 401)

        self.sign_in()
        status, image, _ = self.request(f"/api/admin/projects/{project_id}/image")
        self.assertEqual(status, 200)
        self.assertEqual(image, JPEG)
        status, body, _ = self.request("/api/admin/projects?status=pending")
        self.assertEqual(status, 200)
        project = json.loads(body)[0]
        self.assertEqual(project["id"], project_id)
        status, _, _ = self.request(
            f"/api/admin/projects/{project_id}/moderation",
            "POST",
            json.dumps({"moderation_status": "approved"}).encode(),
            {"Content-Type": "application/json", "X-Admin-Action": "1"},
        )
        self.assertEqual(status, 200)

        restarted_app = EngineeringXApp(self.data_dir, SITE_DIR, "a-long-test-admin-password")
        published = restarted_app.public_projects()
        self.assertEqual(len(published), 1)
        image_name = published[0]["cover_image_path"]
        self.assertEqual((self.data_dir / "uploads" / image_name).read_bytes(), JPEG)
        status, image, headers = self.request(f"/uploads/{image_name}")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assertEqual(image, JPEG)

    def test_only_authenticated_administrator_can_delete_published_projects(self):
        status, body, _ = self.submit()
        self.assertEqual(status, 201)
        project_id = json.loads(body)["id"]
        self.sign_in()
        status, _, _ = self.request(
            f"/api/admin/projects/{project_id}/moderation",
            "POST",
            json.dumps({"moderation_status": "approved"}).encode(),
            {"Content-Type": "application/json", "X-Admin-Action": "1"},
        )
        self.assertEqual(status, 200)

        anonymous = urllib.request.build_opener()
        status, _, _ = self.request(
            f"/api/admin/projects/{project_id}",
            "DELETE",
            headers={"X-Admin-Action": "1"},
            client=anonymous,
        )
        self.assertEqual(status, 401)
        self.assertEqual(len(self.app.public_projects()), 1)

        status, _, _ = self.request(
            f"/api/admin/projects/{project_id}",
            "DELETE",
            headers={"X-Admin-Action": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.app.public_projects(), [])

    def test_rejects_non_image_payload(self):
        body, content_type = multipart(REQUIRED_FIELDS)
        body = body.replace(JPEG, b"not-an-image")
        status, _, _ = self.request(
            "/api/projects",
            "POST",
            body,
            {"Content-Type": content_type},
        )
        self.assertEqual(status, 400)

    def test_server_does_not_expose_source_or_database_files(self):
        for path in ("/server.py", "/tests/test_server.py", "/requirements.txt"):
            with self.subTest(path=path):
                status, _, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_serves_club_logo_as_jpeg_asset(self):
        logo_path = SITE_DIR / "assets" / "engineeringx-club-logo.jpg"
        self.assertTrue(logo_path.is_file())
        status, body, headers = self.request("/assets/engineeringx-club-logo.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assertEqual(body, logo_path.read_bytes())

    def test_accepts_jpeg_png_webp_and_enforces_upload_size(self):
        for image_format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(image_format=image_format):
                image = encoded_image(image_format)
                body, content_type = multipart(REQUIRED_FIELDS, image=image)
                status, _, _ = self.request(
                    "/api/projects",
                    "POST",
                    body,
                    {"Content-Type": content_type},
                )
                self.assertEqual(status, 201)

        oversized = b"\xff\xd8\xff" + b"x" * (5 * 1024 * 1024) + b"\xff\xd9"
        body, content_type = multipart(REQUIRED_FIELDS)
        body = body.replace(JPEG, oversized)
        status, _, _ = self.request(
            "/api/projects",
            "POST",
            body,
            {"Content-Type": content_type},
        )
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()
