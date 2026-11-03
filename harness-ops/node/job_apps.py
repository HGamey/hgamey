"""Observe the selected phones before a write task. The observation never grants or denies a lock."""
import json
import subprocess
import time

from device_scope import device_access, scope


def targets(spec, observation):
    if device_access(spec) != 'write':
        return None
    declared = scope(spec, observation)
    devices = (observation.get('device_scopes') or {}).get('devices', [])
    resources = spec.get('resources', [])
    selected = [value[7:] for value in resources if value.startswith('device:')]
    if declared['scope'] == 'device':
        selected.append(declared['binding']['serial'])
    if not selected or 'global' in resources:
        selected = [device['device_id'] for device in devices]
    bound = declared.get('binding', {})
    if bound.get('legacy_serial'):
        bound = {}
    aliases = {alias: bound['serial'] if device['device_id'] == bound.get('device_id') else device['aliases'][0]
               for device in devices if device.get('aliases')
               for alias in [device['device_id'], *device['aliases']]}
    if bound:
        aliases[bound['device_id']] = bound['serial']
    return sorted({aliases.get(serial, serial) for serial in selected})


def capture(config):
    value = {'at': time.time(), 'devices': [], 'errors': []}
    if not config['serials']:
        value['errors'].append('no device serial was available for the task scope')
    for serial in config['serials']:
        at = time.time()
        try:
            if config['phonefarm'] is None:
                raise ValueError('phonefarm is not configured on this execution node')
            process = subprocess.run([config['phonefarm'], 'app-snapshot', '--serial', serial],
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=7)
            observation = {**json.loads(process.stdout), 'at': at}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            message = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
            observation = {'serial': serial, 'foreground': None, 'running_third_party': None, 'errors': [message]}
        value['devices'].append({**observation, 'at': at})
    return value
