import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NewVersionResumeTests(unittest.TestCase):
    def test_real_script_waits_and_revalidates_without_second_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ['new-version.sh', 'Dockerfile', 'scripts/inference_settings.py',
                'scripts/configure_inference_egress.py', 'scripts/reinstall_agent.py',
                'scripts/wait_foundry_state.py', 'scripts/foundry_agent_lookup.py',
                'scripts/verify_hosted_version.py', 'scripts/write-release-record.py',
                'src/hermes-patches/foundry_inference.py']:
                target = root / relative; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            binaries = root / 'bin'; binaries.mkdir()
            (binaries / 'python3').symlink_to(sys.executable)
            (binaries / 'git').write_text('#!/bin/sh\nif [ "$1" = rev-parse ]; then echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; fi\n')
            (binaries / 'git').chmod(0o700)
            (binaries / 'az').write_text('#!' + sys.executable + '\n' + r'''
import json, os, pathlib, sys
root=pathlib.Path(os.environ['TEST_ROOT']);args=sys.argv[1:]
with (root/'calls').open('a') as f: f.write(' '.join(args)+'\n')
def out(data): print(json.dumps(data))
if args[:3]==['acr','repository','show']: print('sha256:'+'a'*64);sys.exit()
url=args[args.index('--url')+1];method=args[args.index('--method')+1]
if 'inheritablePermissions' in url:
 out({'inheritableScopes':{'scopes':['Chat.ReadWrite','Mail.ReadWrite','Mail.Send','User.Read.All','User.ReadBasic.All','Files.ReadWrite.All','Calendars.ReadWrite','Sites.ReadWrite.All']}})
elif '/deployments?' in url:
 out({'value':[{'name':'model','type':'ModelDeployment','capabilities':{'chatCompletion':True}}]})
elif method=='POST':
 data=json.loads(pathlib.Path(args[args.index('--body')+1][1:]).read_text());data.update(version='4',status='active',blueprint={'client_id':'33333333-3333-3333-3333-333333333333'})
 (root/'created').write_text(json.dumps(data));out({'version':'4'})
elif '/versions/4?' in url:
 data=json.loads((root/'created').read_text())
 if not (root/'polled').exists(): data['status']='creating';(root/'polled').touch()
 out(data)
elif '/versions/3?' in url:
 out({'version':'3','status':'active','blueprint':{'client_id':'33333333-3333-3333-3333-333333333333'},'metadata':{'enableVnextExperience':'true'},'definition':{
 'kind':'hosted','cpu':'2','memory':'4Gi','image':'reg.azurecr.io/hermes-full-foundry@sha256:'+'b'*64,
 'container_protocol_versions':[{'protocol':'activity_protocol','version':'v1'},{'protocol':'responses','version':'2.0.0'}],
 'environment_variables':{'AgentBlueprintId':'33333333-3333-3333-3333-333333333333','MODEL_DEPLOYMENT_NAME':'model','FALLBACK_MODEL_DEPLOYMENT_NAME':'model'}}})
else: print('3')
''')
            (binaries / 'az').chmod(0o700)
            env = {key: value for key, value in os.environ.items() if not key.startswith(('INFERENCE_', 'NOUS_', 'MODEL_', 'PRIMARY_', 'FALLBACK_', 'RAI_'))}
            env.update(PATH=str(binaries)+os.pathsep+env['PATH'], TEST_ROOT=str(root), ACCOUNT='example-account',
                PROJECT='example-project', AGENT='example-agent', REG='reg', HERMES_DEPLOY_SKIP_LOCAL_CONFIG='true',
                HERMES_VERSION_RESULT_FILE=str(root/'result.json'), HERMES_READY_POLL_INTERVAL='0.01', HERMES_READY_TIMEOUT='5')
            # Interrupt after POST while the version is still creating; preserve its ID.
            first = subprocess.run(['bash', str(root/'new-version.sh'), 'tag'],
                env=dict(env, HERMES_READY_TIMEOUT='0'), text=True, capture_output=True)
            self.assertNotEqual(0, first.returncode)
            self.assertIn('Timed out waiting', first.stderr)
            self.assertFalse(json.loads((root/'result.json').read_text())['verified'])
            for attempt in range(2):
                result = subprocess.run(['bash', str(root/'new-version.sh'), 'tag'], env=env, text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue(json.loads((root/'result.json').read_text())['verified'])
            self.assertEqual(1, (root/'calls').read_text().count('--method POST'))
            self.assertIn('resuming validation', result.stdout)
