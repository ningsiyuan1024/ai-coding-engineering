from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factory import addy_codex
from factory.runtime import RuntimeUnavailable


class AddyCodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pin = "a" * 40
        self.source = self.tmp / "upstream" / "native" / "addy" / self.pin / "plugin"
        self.home = self.tmp / "addy" / "codex" / self.pin
        self.cache = self.home / "plugins/cache/agent-skills/agent-skills/1.0.0"
        manifest = self.source / ".codex-plugin/plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name":"agent-skills","version":"1.0.0","skills":"./skills/"}))
        skill = self.source / "skills/spec/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("fixture skill")
        (self.source / "README.md").write_text("fixture documentation")
        self.enterContext(patch.object(addy_codex, "open_pinned", return_value=SimpleNamespace(
            plugin_root=self.source, source=SimpleNamespace(repository="addyosmani/agent-skills"),
            skills={"spec":skill})))
        test = self
        class Runtime:
            version = "fixture"
            def __init__(self, home, cwd, **kwargs):
                self.cwd = Path(cwd)
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                pass
            def _rpc(self, name, params):
                test.assertEqual((name, params["marketplaceKinds"], params["forceRefetch"]),
                                 ("plugin/list", ["local"], False))
                if not test.cache.exists():
                    return {"marketplaces":[]}
                record = {"id":addy_codex.PLUGIN, "source":{"type":"local", "path":str(test.source)},
                          "installed":True,"enabled":True}
                return {"marketplaces":[{"plugins":[record]}]}
            def skills(self):
                if not test.cache.exists():
                    return []
                return [{"name":"agent-skills:spec","enabled":True,"pluginId":addy_codex.PLUGIN,
                         "path":str(test.cache / "skills/spec/SKILL.md")}]
            isolate_skills = skills
        self.runtime = Runtime
        self.enterContext(patch.object(addy_codex, "CodexRuntime", Runtime))
        def command(_, args):
            if args[:2] == ["marketplace", "add"]:
                self.assertEqual(Path(args[2]), self.source)
                return {"marketplaceName":"agent-skills", "installedRoot":str(self.source)}
            self.assertEqual(args, ["add", addy_codex.PLUGIN])
            shutil.copytree(self.source, self.cache)
            return {"pluginId":addy_codex.PLUGIN,"version":"1.0.0"}
        self.command = self.enterContext(patch.object(addy_codex, "_command", side_effect=command))

    def test_pinned_marketplace_install_and_reopen(self):
        first = addy_codex.provision(self.home, self.source, self.pin)
        self.assertEqual(first["before"], {"installed":False,"enabled":False,"skills":0})
        self.assertEqual(first["after"], {"installed":True,"enabled":True,"skills":1})
        second = addy_codex.provision(self.home, self.source, self.pin)
        self.assertEqual(second["verified_files"], 3)
        self.assertEqual(second["skills"], ["spec"])
        self.assertEqual(second["native_skill_names"], ["agent-skills:spec"])
        self.assertEqual(self.command.call_count, 2)

    def test_reopen_checks_files_beyond_skill_entrypoints(self):
        addy_codex.provision(self.home, self.source, self.pin)
        (self.cache / "README.md").write_text("changed document")
        with self.assertRaises((RuntimeUnavailable, ValueError)):
            addy_codex.provision(self.home, self.source, self.pin)

    def test_discovery_cannot_point_to_a_different_profile(self):
        addy_codex.provision(self.home, self.source, self.pin)
        other = {"name":"agent-skills:spec", "enabled":True, "pluginId":addy_codex.PLUGIN,
                 "path":str(self.source / "skills/spec/SKILL.md")}
        with patch.object(self.runtime, "isolate_skills", return_value=[other]), \
                self.assertRaisesRegex(RuntimeUnavailable, "discovery differs"):
            addy_codex.provision(self.home, self.source, self.pin)

    def test_home_must_be_scoped_to_addy_host_and_revision(self):
        for home in (self.tmp / "matt/codex" / self.pin, self.home.with_name("b" * 40),
                     self.tmp / "addy/other" / self.pin):
            with self.subTest(home=home), self.assertRaisesRegex(ValueError, "dedicated versioned"):
                addy_codex.provision(home, self.source, self.pin)
            self.assertFalse(home.exists())

    def test_unrelated_enabled_plugin_denied_before_host_is_opened(self):
        self.home.mkdir(parents=True)
        (self.home / "config.toml").write_text('[plugins."unrelated@third-party"]\nenabled = true\n')
        with patch.object(addy_codex, "CodexRuntime") as host, self.assertRaisesRegex(RuntimeUnavailable, "Another plugin"):
            addy_codex.provision(self.home, self.source, self.pin)
        host.assert_not_called()

    def test_rejects_another_enabled_plugin_without_any_skill(self):
        plugins = [{"id":addy_codex.PLUGIN, "source":{"type":"local", "path":str(self.source)},
                    "installed":True, "enabled":True}, {"id":"other@local", "enabled":True}]
        with patch.object(self.runtime, "_rpc", return_value={"marketplaces":[{"plugins":plugins}]}), \
                self.assertRaisesRegex(RuntimeUnavailable, "Another plugin"):
            addy_codex.provision(self.home, self.source, self.pin)
        self.command.assert_not_called()

    def test_concurrent_workers_install_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(addy_codex.provision, self.home, self.source, self.pin) for _ in range(2)]
            receipts = [future.result() for future in futures]
        self.assertEqual(self.command.call_count, 2)
        self.assertEqual(sorted(item["before"]["skills"] for item in receipts), [0, 1])

    def test_reopen_same_immutable_revision_from_another_source_mirror(self):
        addy_codex.provision(self.home, self.source, self.pin)
        mirror = self.tmp / "mirror/native/addy" / self.pin / "plugin"
        shutil.copytree(self.source, mirror)
        original = addy_codex.open_pinned.return_value
        def lookup(_, profile_root, revision):
            self.assertEqual(revision, self.pin)
            if profile_root == mirror.parents[3]:
                return SimpleNamespace(plugin_root=mirror, source=original.source,
                                       skills={"spec":mirror / "skills/spec/SKILL.md"})
            return original
        with patch.object(addy_codex, "open_pinned", side_effect=lookup):
            receipt = addy_codex.provision(self.home, mirror, self.pin)
        self.assertEqual(Path(receipt["source"]), mirror)
        self.assertEqual(self.command.call_count, 2)
