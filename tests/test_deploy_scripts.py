import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import shlex


ROOT = Path(__file__).resolve().parents[1]
HEALTHCHECK = ROOT / "deploy" / "healthcheck-workbench.sh"
PUBLIC_RELEASE_CHECK = ROOT / "deploy" / "verify-public-release.sh"
AUDIT_CREDENTIALS = ROOT / "deploy" / "audit-credentials.sh"
DEPLOY = ROOT / "deploy" / "deploy-workbench.sh"
SERVICE_FILES = list((ROOT / "deploy").glob("workbench*.service"))
TIMER_FILES = list((ROOT / "deploy").glob("workbench*.timer"))


class DeployScriptTests(unittest.TestCase):

    def test_public_release_check_allows_anonymous_auth_gate_but_can_require_api(self):
        server_code = r'''
import http.server
import socketserver
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/static/workbench.html":
            body, status = b"v0.3.101", 200
        elif self.path == "/static/sw.js":
            body, status = b"workbench-shell-v0.3.101", 200
        elif self.path == "/api/meta":
            body, status = b"auth required", 401
        else:
            body, status = b"not found", 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass

with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
    print(server.server_address[1], flush=True)
    server.serve_forever()
'''
        server = subprocess.Popen([sys.executable, "-u", "-c", server_code], stdout=subprocess.PIPE, text=True)
        try:
            port = int(server.stdout.readline().strip())
            base = f"http://127.0.0.1:{port}"
            allowed = subprocess.run(
                ["bash", str(PUBLIC_RELEASE_CHECK), "--url", base, "--expected", "0.3.101"],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            required = subprocess.run(
                ["bash", str(PUBLIC_RELEASE_CHECK), "--url", base, "--expected", "0.3.101", "--require-api"],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
        finally:
            server.terminate()
            server.wait(timeout=5)
            if server.stdout:
                server.stdout.close()
        self.assertEqual(allowed.returncode, 0)
        self.assertIn("api-meta=auth_required status=401", allowed.stdout)
        self.assertIn("release-check=ok", allowed.stdout)
        self.assertEqual(required.returncode, 1)
        self.assertIn("api-meta authentication required", required.stdout)
    def test_deploy_validates_release_checker_is_present(self):
        source = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("verify-public-release.sh", source)
        self.assertIn("PUBLIC_RELEASE_URL", source)
        self.assertIn("--expected \"$expected_version\"", source)
        self.assertIn("--skip-release-check", source)
        self.assertIn("audit-credentials.sh", source)
        self.assertIn("observe-workbench.sh", source)
        self.assertIn("--exclude=.cache/", source)

    def test_start_and_verify_calls_public_release_gate_with_target_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            deploy_dir = target / "deploy"
            markers = root / "markers"
            deploy_dir.mkdir(parents=True)
            markers.mkdir()
            (target / "VERSION").write_text("0.3.130\n", encoding="utf-8")
            health = deploy_dir / "healthcheck-workbench.sh"
            health.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > " + shlex.quote(str(markers / "health.args")) + "\n",
                encoding="utf-8",
            )
            release = deploy_dir / "verify-public-release.sh"
            release.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > " + shlex.quote(str(markers / "release.args")) + "\n",
                encoding="utf-8",
            )
            health.chmod(0o755)
            release.chmod(0o755)
            shell = f"""
source {shlex.quote(str(DEPLOY))}
TARGET_DIR={shlex.quote(str(target))}
PUBLIC_RELEASE_URL=http://release.example.test
PUBLIC_URL=
SKIP_NGINX=1
SKIP_PUBLIC_RELEASE=0
systemctl() {{ return 0; }}
start_and_verify
"""
            result = subprocess.run(["bash", "-c", shell], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--url http://release.example.test --expected 0.3.130", (markers / "release.args").read_text(encoding="utf-8"))

    def test_credential_audit_help_does_not_require_or_print_secrets(self):
        result = subprocess.run(
            ["bash", str(AUDIT_CREDENTIALS), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("只读检查 Workbench 的 LLM 配置来源和文件权限", result.stdout)
        self.assertNotIn("LLM_API_KEY=", result.stdout)

    def test_all_workbench_services_share_optional_secret_environment(self):
        self.assertEqual(len(SERVICE_FILES), 6)
        for service_file in SERVICE_FILES:
            source = service_file.read_text(encoding="utf-8")
            self.assertIn("EnvironmentFile=-/www/workbench/.env", source, service_file.name)
        self.assertIn("Environment=WORKBENCH_CLOUD_WORKSPACES=/www/workbench", (ROOT / "deploy" / "workbench.service").read_text(encoding="utf-8"))
        self.assertEqual([path.name for path in TIMER_FILES], ["workbench-observer.timer"])

    def test_custom_target_rewrites_service_and_cloud_workspace_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = "/srv/workbench-release"
            script = f"""
source {shlex.quote(str(DEPLOY))}
TARGET_DIR={shlex.quote(target)}
RUN_USER=workbench
RUN_GROUP=workbench
for template in {shlex.quote(str(ROOT / 'deploy'))}/workbench*.service {shlex.quote(str(ROOT / 'deploy'))}/workbench*.timer; do
    output={shlex.quote(temp_dir)}/$(basename "$template")
    render_service_template "$template" "$output"
    cat "$output"
done
"""
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("/www/workbench", result.stdout)
        self.assertIn(f"WorkingDirectory={target}", result.stdout)
        self.assertIn(f"EnvironmentFile=-{target}/.env", result.stdout)
        self.assertIn(f"Environment=WORKBENCH_CLOUD_WORKSPACES={target}", result.stdout)
        self.assertIn(f"{target}/crawl_worker.py", result.stdout)
        self.assertIn(f"{target}/monitor_worker.py", result.stdout)
        self.assertIn(f"{target}/sync_worker.py", result.stdout)
        self.assertIn(f"{target}/agent_worker.py", result.stdout)

    def test_observer_is_read_only_and_hardened(self):
        source = (ROOT / "deploy" / "observe-workbench.sh").read_text(encoding="utf-8")
        self.assertIn("只读记录", source)
        self.assertIn("不保存 API 响应正文或凭据", source)
        self.assertIn("Retry only the", source)
        self.assertIn("range(2)", source)
        service = (ROOT / "deploy" / "workbench-observer.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ReadWritePaths=/www/workbench/data", service)

    def test_public_release_check_rejects_credentials_and_query_strings(self):
        for value in ("https://user@example.com", "https://example.com?token=secret", "https://example.com/#version"):
            result = subprocess.run(
                ["bash", str(PUBLIC_RELEASE_CHECK), "--url", value],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("线上地址", result.stderr)

    def test_public_release_check_help_is_read_only(self):
        result = subprocess.run(
            ["bash", str(PUBLIC_RELEASE_CHECK), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("只读核对线上公开静态资源", result.stdout)

    def test_healthcheck_accepts_documented_zero_wait(self):
        result = subprocess.run(
            [
                "bash",
                str(HEALTHCHECK),
                "--skip-public",
                "--host",
                "127.0.0.1",
                "--port",
                "1",
                "--timeout",
                "1",
                "--wait",
                "0",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        # Port 1 is deliberately not expected to be Workbench. The test is
        # checking argument validation: the script must reach the read-only
        # probe instead of rejecting its documented default wait value.
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("等待必须是", result.stderr)
        self.assertIn("result=fail", result.stdout)


if __name__ == "__main__":
    unittest.main()
