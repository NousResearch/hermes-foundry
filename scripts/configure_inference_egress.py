"""Allow configured Nous inference through Foundry egress without relaxing other rules.

Reads a proposed version body, clones any existing RAI policy, and attaches a verified
policy to that body. Never changes the serving version or a shared source policy.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inference_settings import settings

API = '2026-05-15-preview'


def wait_readback(read, validate, attempts=12, sleep=time.sleep):
    for attempt in range(attempts):
        actual = read()  # Authorization/read errors are not mistaken for stale state.
        try:
            validate(actual)
            return actual
        except RuntimeError:
            if attempt == attempts - 1:
                raise
            print('Waiting for egress configuration readback to converge...', flush=True)
            sleep(5)


def destinations(env):
    _, allowed, endpoint, _ = settings(env)
    if 'nous' not in allowed:
        return []
    hosts = [urlsplit(endpoint).hostname]
    hosts.append('portal.nousresearch.com')
    return hosts


def policy_with_allows(source, hosts):
    properties = copy.deepcopy(source.get('properties') or {
        'basePolicyName': 'Microsoft.DefaultV2', 'mode': 'Blocking', 'contentFilters': []})
    properties.pop('provisioningState', None)
    # If no outbound restrictions existed, don't introduce a fleet-wide deny policy
    # that would break existing tools. Restrictive existing defaults stay restrictive.
    egress = properties.setdefault('egressPolicy', {
        'mode': 'Enforced', 'defaultAction': 'Allow', 'rules': []})
    rules = egress.setdefault('rules', [])
    names = {'hermes-inference-' + hashlib.sha256(h.encode()).hexdigest()[:12] for h in hosts}
    egress['rules'] = [
        {'name': 'hermes-inference-' + hashlib.sha256(host.encode()).hexdigest()[:12],
         'ruleType': 'Fqdn', 'match': {'host': host}, 'action': {'actionType': 'Allow'}}
        for host in hosts
    ] + [rule for rule in rules if rule.get('name') not in names]
    if len(egress['rules']) > 480:
        raise ValueError('Adding inference destinations exceeds the 480-rule policy limit')
    return {'properties': properties}


def az(*args):
    result = subprocess.run(['az', *args, '-o', 'json'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout or '{}')


def rest(method, resource_id, api, body=None):
    args = ['rest', '--method', method, '--url',
            f'https://management.azure.com{resource_id}?api-version={api}']
    if body is None:
        return az(*args)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'body.json'
        path.write_text(json.dumps(body))
        return az(*args, '--body', '@' + str(path))


def configure(body, account):
    definition = body['definition']
    hosts = destinations(definition.get('environment_variables') or {})
    if not hosts:
        return body
    accounts = az('cognitiveservices', 'account', 'list')
    matches = [a for a in accounts if a['name'] == account]
    if len(matches) != 1:
        raise ValueError('Expected exactly one matching Foundry account in the selected subscription')
    account_id = matches[0]['id']
    # List rather than treating every failed GET as "no network": auth/API errors must fail closed.
    try:
        networks = rest('GET', account_id + '/managedNetworks', '2026-05-15-preview').get('value', [])
    except RuntimeError as exc:
        # Legacy hosted-only accounts have no AML capability host. Azure returns this
        # specific missing-backend error instead of an empty managed-network list.
        message = str(exc)
        if ('Resource referenced by capabilityHost' not in message
                or 'not found' not in message or 'NotFoundError' not in message):
            raise
        print('No managed-network capability host exists for this account.')
        networks = []
    for network in networks:
        managed = network.get('properties', {}).get('managedNetwork', {})
        mode = managed.get('isolationMode')
        if mode == 'AllowOnlyApprovedOutbound':
            for host in hosts:
                name = 'hermes-inference-' + hashlib.sha256(host.encode()).hexdigest()[:12]
                rule_id = network['id'] + '/outboundRules/' + name
                rule_body = {'properties': {'type': 'FQDN', 'category': 'UserDefined',
                                             'destination': host}}
                rest('PUT', rule_id, '2026-05-15-preview', rule_body)
                def validate_rule(check):
                    if (check.get('properties', {}).get('destination') != host
                            or check.get('properties', {}).get('type') != 'FQDN'):
                        raise RuntimeError('Managed-network FQDN rule readback mismatch')
                wait_readback(lambda: rest('GET', rule_id, '2026-05-15-preview'), validate_rule)
    rai = definition.setdefault('rai_config', {})
    source_id = os.environ.get('RAI_POLICY_ID') or rai.get('rai_policy_name')
    source = {}
    if source_id:
        if not source_id.lower().startswith((account_id + '/raiPolicies/').lower()):
            raise ValueError('RAI policy must belong to the target Foundry account')
        source = rest('GET', source_id, API)
    policy = policy_with_allows(source, hosts)
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:16]
    policy_id = account_id + '/raiPolicies/hermes-inference-' + digest
    rest('PUT', policy_id, API, policy)
    wait_readback(lambda: rest('GET', policy_id, API), lambda actual: verify_policy(policy, actual))
    rai['rai_policy_name'] = policy_id
    print('Configured inference egress destinations: ' + ', '.join(hosts))
    return body


def verify_policy(expected, actual):
    properties = actual.get('properties', {})
    for key, value in expected['properties'].items():
        if key == 'contentFilters':
            # Azure materializes inherited filters and default fields on readback.
            # Every explicitly requested filter field must still match.
            for requested in value:
                matching = [f for f in properties.get(key, [])
                            if f.get('name') == requested.get('name')
                            and f.get('source') == requested.get('source')]
                if not any(all(f.get(k) == v for k, v in requested.items()) for f in matching):
                    raise RuntimeError('RAI policy content filter readback mismatch')
        elif properties.get(key) != value:
            raise RuntimeError('RAI policy readback mismatch: ' + key)


def verify(expected, actual):
    if expected['definition'].get('rai_config') != actual['definition'].get('rai_config'):
        raise RuntimeError('Agent version did not retain its RAI policy; do not route traffic')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--body', required=True)
    parser.add_argument('--account')
    parser.add_argument('--verify')
    args = parser.parse_args()
    body = json.loads(Path(args.body).read_text())
    if args.verify:
        verify(body, json.loads(Path(args.verify).read_text()))
    else:
        if not args.account:
            parser.error('--account is required when configuring egress')
        configure(body, args.account)
        Path(args.body).write_text(json.dumps(body))


if __name__ == '__main__':
    main()
