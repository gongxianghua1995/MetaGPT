"""Offline launcher regressions; no models, Docker daemon or benchmark runs."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('launcher', Path(__file__).with_name('swe_offline.py'))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_jsonl_preserves_unicode_line_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'data.jsonl'
            p.write_text(json.dumps({'text':'a\u2028b'},ensure_ascii=False)+'\n')
            self.assertEqual(launcher.read_rows(p),[{'text':'a\u2028b'}])

    def test_cli_probe_checks_real_parser_but_stops_before_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'entry.py';sentinel=Path(tmp)/'SHOULD_NOT_EXIST'
            script.write_text("import argparse\nfrom pathlib import Path\np=argparse.ArgumentParser()\np.add_argument('--budget',type=int,required=True)\np.parse_args()\nPath("+repr(str(sentinel))+").touch()\n")
            launcher.cli_check([sys.executable,str(script),'--budget','20'],os.environ.copy())
            self.assertFalse(sentinel.exists())
            with self.assertRaises(RuntimeError):
                launcher.cli_check([sys.executable,str(script),'--budget','oops'],os.environ.copy())
            with self.assertRaises(RuntimeError):
                launcher.cli_check([sys.executable,str(script),'--unknown'],os.environ.copy())
            self.assertFalse(sentinel.exists())

    def test_failed_import_does_not_echo_provider_text(self):
        completed=SimpleNamespace(returncode=1,stdout='',stderr='API_KEY=PRIVATE_SENTINEL')
        with patch.object(launcher.subprocess,'run',return_value=completed):
            with self.assertRaises(RuntimeError) as error:
                launcher.checked([sys.executable,'-c','pass'],os.environ.copy())
        self.assertNotIn('PRIVATE_SENTINEL',str(error.exception))

    def test_missing_split_ids_cannot_silently_shrink_experiment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);split=root/'scripts/swe_offline_ids';split.mkdir(parents=True)
            (split/'verified.txt').write_text('django__django-1\ndjango__django-2\n')
            data=root/'data.json';data.write_text(json.dumps([{'instance_id':'django__django-1'}]))
            args=SimpleNamespace(data=data,benchmark='verified',domain=None,limit_per_domain=1)
            with patch.object(launcher,'ROOT',root),self.assertRaisesRegex(ValueError,'lacks 1 IDs'):
                launcher.select_rows(args)

    def test_duplicate_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp)/'data.json'
            data.write_text(json.dumps([{'instance_id':'same'},{'instance_id':'same'}]))
            with self.assertRaisesRegex(ValueError,'unique'):
                launcher.select_rows(SimpleNamespace(data=data))

    def test_pro_image_tag_is_bounded(self):
        row={'instance_id':'task','dockerhub_tag':'a'*160}
        self.assertEqual(launcher.images_for([row],'pro'),['jefzda/sweap-images:'+'a'*128])

    def test_resolved_python_preserves_virtualenv_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'python';p.symlink_to(sys.executable)
            self.assertEqual(launcher.interpreter(str(p)),str(p))


if __name__=='__main__':
    unittest.main()
