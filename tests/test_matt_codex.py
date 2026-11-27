from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factory import matt_codex


class MattCodexTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(dir=Path.cwd()))).resolve()
        self.rev = "e" * 40
        self.home = self.root / "matt/codex" / self.rev
        self.source = self.root / "immutable"
        skill = self.source / "skills/engineering/ask-matt"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"upstream skill\r\n")
        (skill / "verification.md").write_bytes(b"upstream companion")
        self.profile = SimpleNamespace(plugin_root=self.source, skills={"ask-matt":skill/"SKILL.md"},
                                       version="1.0", source=SimpleNamespace(tree="f"*40))
        self.cli_calls = []

    def native_cli(self, home, args):
        self.cli_calls.append(args)
        if args[:2] == ["marketplace","add"]:
            self.assertEqual(Path(args[2]),self.source)
            return {"marketplaceName":"mattpocock","installedRoot":args[2]}
        self.assertEqual(args, ["add","mattpocock-skills@mattpocock"])
        plugin = home / "plugins/cache/mattpocock/mattpocock-skills/1.0"
        shutil.copytree(self.source, plugin)
        (home/"config.toml").write_text(
            '[marketplaces.mattpocock]\nsource_type="local"\nsource=' + json.dumps(str(self.source)) + '\n'
            '[plugins."mattpocock-skills@mattpocock"]\nenabled=true\n',encoding="utf-8")
        return {"pluginId":matt_codex.PLUGIN_ID,"version":"1.0","installedPath":str(plugin)}

    def provision(self):
        return matt_codex.provision(self.home, self.source, self.rev)

    def test_native_install_is_serialized_idempotent_and_keeps_subordinate_files(self):
        with patch.object(matt_codex, "_source", return_value=self.profile), \
             patch.object(matt_codex, "_command", side_effect=self.native_cli):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.map(lambda _: self.provision(), range(2))
            self.assertEqual(first, second)
            self.assertEqual(len(self.cli_calls), 2)
            self.assertEqual(first["skills"], ["ask-matt"])
            self.assertEqual(first["native_skill_names"], ["mattpocock-skills:ask-matt"])
            extra = Path(first["installed_path"]) / "skills/engineering/ask-matt/verification.md"
            extra.write_bytes(b"modified")
            with self.assertRaises(ValueError):
                self.provision()
            self.assertEqual(len(self.cli_calls), 2)

    def test_unversioned_or_foreign_roots_fail_before_host_changes(self):
        with patch.object(matt_codex,"_command") as cli:
            for home,source,revision in [(self.root,self.source,self.rev),
                                         (self.home,self.source,"main"),
                                         (self.home,self.source,self.rev)]:
                with self.assertRaises(ValueError):
                    matt_codex.provision(home,source,revision)
            cli.assert_not_called()

    def test_remote_marketplace_cannot_replace_the_task_pin(self):
        self.home.mkdir(parents=True)
        (self.home/"config.toml").write_text('[marketplaces.mattpocock]\nsource_type="git"\n')
        with patch.object(matt_codex, "_source", return_value=self.profile), \
             patch.object(matt_codex, "_command") as cli, self.assertRaisesRegex(ValueError,"unpinned"):
            self.provision()
        cli.assert_not_called()
