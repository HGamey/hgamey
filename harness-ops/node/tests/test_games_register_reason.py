"""games register says why it failed: the node's own reason for refusing a manifest, and the system's reason a manifest
file could not be read. A game installed from its APK alone registers once it leaves the empty deployment out."""
import json
import unittest

import test_node_games as fixture


class GamesRegisterReasonTests(unittest.TestCase):
    setUp, tearDown, cli = fixture.GameFlowTests.setUp, fixture.GameFlowTests.tearDown, fixture.GameFlowTests.cli
    manifest = fixture.GameFlowTests.manifest

    def test_a_refused_manifest_shows_the_nodes_reason(self):
        sha256 = self.manifest()["assets"][0]["sha256"]
        manifest = self.manifest(
            installation={"schema": "phonefarm/game-install-recipe@1", "package": "com.example.game", "version_code": 1,
                          "apk_sha256": sha256, "apk_asset_name": "data"},
            deployment={"schema": "phonefarm/game-resources-recipe@1", "package": "com.example.game",
                        "version_code": 1, "apk_sha256": sha256, "files": []})
        source = self.root / "manifest.json"
        source.write_text(json.dumps(manifest))
        refused = self.cli("register", str(source))
        self.assertEqual(refused.returncode, 2)
        self.assertIn("节点请求失败（HTTP 400，invalid_request）：deployment files must contain 1–64 entries; "
                      "a game installed from its APK alone omits deployment and keeps installation", refused.stderr)
        del manifest["deployment"]
        source.write_text(json.dumps(manifest))
        registered = self.cli("register", str(source))
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(json.loads(registered.stdout)["manifest"]["installation"]["apk_asset_name"], "data")

    def test_an_unreadable_manifest_file_says_why(self):
        missing = self.root / "gi-manifest.json"
        result = self.cli("register", str(missing))
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"无法读取清单文件 {missing}：No such file or directory", result.stderr)
        result = self.cli("register", str(self.root))
        self.assertIn(f"无法读取清单文件 {self.root}：Is a directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
