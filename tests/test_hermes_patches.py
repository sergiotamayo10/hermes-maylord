"""Local patches to Hermes: the image applies every patches/*.patch after cloning."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHES = sorted((ROOT / "patches").glob("*.patch"))


class HermesPatchesTest(unittest.TestCase):
    def test_dockerfile_applies_patches_after_clone(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        clone = dockerfile.index("git clone --depth 1")
        copy = dockerfile.index("COPY patches/ /app/patches/")
        apply = dockerfile.index('git -C /opt/hermes/repo apply --check "$p"')
        self.assertLess(clone, copy)
        self.assertLess(copy, apply)
        self.assertIn("|| exit 1", dockerfile[apply:apply + 200])

    def test_modal_revive_patch_content(self):
        patch = (ROOT / "patches" / "modal-sandbox-revive.patch").read_text(encoding="utf-8")
        self.assertIn("+++ b/tools/environments/modal.py", patch)
        self.assertEqual(patch.count("+++ b/"), 1)
        self.assertIn('-                    timeout=int(create_kwargs.pop("timeout", 3600))', patch)
        self.assertIn("TERMINAL_MODAL_SANDBOX_LIFETIME", patch)
        self.assertIn("+        self._revive_if_dead()", patch)

    @unittest.skipUnless(os.environ.get("HERMES_UPSTREAM_DIR"),
                         "set HERMES_UPSTREAM_DIR to a clean v0.22.2 checkout")
    def test_patches_apply_to_upstream(self):
        upstream = os.environ["HERMES_UPSTREAM_DIR"]
        for patch in PATCHES:
            with self.subTest(patch=patch.name):
                subprocess.run(["git", "-C", upstream, "apply", "--check", str(patch)], check=True)


if __name__ == "__main__":
    unittest.main()
