"""Old workflows must not reach dependency setup or external writes by default."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/changes-sync/action.yml'


class ChangesSyncOptInTest(unittest.TestCase):
    def test_default_and_false_skip_every_executable_step(self):
        action = yaml.safe_load(ACTION.read_text())
        default = action['inputs']['notion-sync-enabled']['default']
        self.assertEqual(default, 'false')
        for value in (default, 'false', ''):
            with self.subTest(value=value):
                self.assertEqual(self.execute(action, value), [])

    def test_explicit_opt_in_runs_dependency_setup_and_sync(self):
        action = yaml.safe_load(ACTION.read_text())
        self.assertEqual(self.execute(action, 'true'), ['python3', 'sync'])

    def execute(self, action, enabled):
        # Execute the actual composite run blocks against fake tools/action scripts.
        # A step added without an opt-in condition will execute and fail this test.
        steps = action['runs']['steps']
        conditions = [step.get('if', 'true') for step in steps]
        evaluator = '''
const fs = require('fs'); const {conditions, enabled} = JSON.parse(fs.readFileSync(0, 'utf8'));
const inputs = {'notion-sync-enabled': enabled};
process.stdout.write(JSON.stringify(conditions.map(expression => new Function('inputs', 'return (' + expression + ')')(inputs))));
'''
        selected = json.loads(subprocess.check_output(
            ['node', '-e', evaluator], input=json.dumps({'conditions': conditions, 'enabled': enabled}), text=True))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / 'calls'
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            for tool in ('jq', 'python3', 'curl', 'git'):
                path = bin_dir / tool
                path.write_text('#!/bin/bash\nprintf "%s\\n" "' + tool + '" >> "$CALLS"\n')
                path.chmod(0o755)
            script = root / 'run.sh'
            script.write_text('#!/bin/bash\nprintf "sync\\n" >> "$CALLS"\n')
            script.chmod(0o755)
            env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'], 'CALLS': str(calls)}
            for selected_step, step in zip(selected, steps):
                if selected_step:
                    source = step['run'].replace('${{ github.action_path }}', str(root))
                    subprocess.run(['/bin/bash', '-eo', 'pipefail', '-c', source], env=env, cwd=root, check=True)
            return calls.read_text().splitlines() if calls.exists() else []


if __name__ == '__main__':
    unittest.main()
