import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage_build', ROOT / 'scripts/stage-build-context.py')
stager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stager)


class BuildContextTests(unittest.TestCase):
    def test_real_manifest_stages_exact_reviewed_files(self):
        manifest = json.loads((ROOT / 'config/build-context.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            stager.stage(output)
            actual = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
            self.assertEqual(actual, set(manifest) | {'.dockerignore'})
            for name in manifest:
                self.assertEqual((output / name).read_bytes(), (ROOT / name).read_bytes())
        rules = (ROOT / '.dockerignore').read_text().splitlines()
        explicit_files = {r[1:] for r in rules if r.startswith('!') and not r.endswith('/')}
        self.assertEqual(explicit_files, set(manifest))
        self.assertIn('**', rules)

    def test_unlisted_secrets_never_enter_staged_context(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as output:
            root = pathlib.Path(directory)
            (root / 'config').mkdir()
            (root / 'config/build-context.json').write_text('["Dockerfile"]')
            (root / 'Dockerfile').write_text('FROM scratch\n')
            (root / '.dockerignore').write_text('**\n!Dockerfile\n')
            for name in ('.env', '.private/notes.md', 'secrets/test.pem', 'src/edge/local-secret.pfx'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('SYNTHETIC DATA')
            with mock.patch.object(stager, 'ROOT', root):
                stager.stage(output)
            self.assertEqual({p.name for p in pathlib.Path(output).iterdir()}, {'Dockerfile', '.dockerignore'})

    def test_symlink_inputs_and_parent_directories_fail_closed(self):
        for parent_link in (False, True):
            with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as output:
                root = pathlib.Path(directory)
                (root / 'config').mkdir()
                (root / 'outside').mkdir()
                (root / 'outside/source.py').write_text('synthetic')
                (root / 'config/build-context.json').write_text('["src/source.py"]')
                if parent_link:
                    (root / 'src').symlink_to(root / 'outside', target_is_directory=True)
                else:
                    (root / 'src').mkdir()
                    (root / 'src/source.py').symlink_to(root / 'outside/source.py')
                with mock.patch.object(stager, 'ROOT', root), self.assertRaisesRegex(ValueError, 'Symlink'):
                    stager.stage(output)

    def test_build_script_sends_staged_context_and_cleans_it(self):
        import os
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = json.loads((ROOT / 'config/build-context.json').read_text())
            for name in manifest + ['.dockerignore', 'build.sh', 'scripts/stage-build-context.py', 'config/build-context.json']:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            (root / '.env').write_text('SYNTHETIC DATA')
            (root / 'src/edge/local-secret.pfx').write_text('SYNTHETIC DATA')
            # azd inputs must not be overridden by stale settings from another installation.
            (root / '.deployment.local').write_text('REG=wrong-registry\n')
            binaries = root / 'bin'
            binaries.mkdir()
            log = root / 'build-call.json'
            az = binaries / 'az'
            az.write_text('#!' + __import__('sys').executable + '''
import json, pathlib, sys
context = pathlib.Path(sys.argv[-1])
assert context != pathlib.Path.cwd()
assert not (context / '.env').exists()
assert not (context / 'src/edge/local-secret.pfx').exists()
assert (context / 'src/sidecar/secure_http.py').is_file()
assert sys.argv[sys.argv.index('--registry') + 1] == 'expected-registry'
pathlib.Path('build-call.json').write_text(json.dumps({'context': str(context)}))
''')
            az.chmod(0o700)
            result = subprocess.run(['bash', 'build.sh', 'security-test'], cwd=root,
                env={**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                     'REG': 'expected-registry', 'HERMES_BUILD_SKIP_LOCAL_CONFIG': 'true'},
                capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(pathlib.Path(json.loads(log.read_text())['context']).exists())
