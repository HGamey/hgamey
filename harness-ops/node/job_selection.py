"""Choose a phone once from observations; the selected node owns admission and locks."""
from urllib.parse import quote, unquote

from device_scope import conflicts, serial_from_argv


def phones(node_id, name, snapshot, connected):
    fleet, queue = snapshot.get('fleet') or {}, snapshot.get('queue') or {}
    scopes = (queue.get('lease') or {}).get('device_scopes') or {}
    result = {}
    observed = list(fleet.get('devices') or [])
    seen = {phone['serial'] for phone in observed}
    for registered in scopes.get('devices', []):
        if not seen.intersection(registered.get('aliases', [])):
            observed.append({'serial': registered['aliases'][0], 'model': None})
    for phone in observed:
        serial = phone['serial']
        registered = next((item for item in scopes.get('devices', [])
                           if serial == item['device_id'] or serial in item.get('aliases', [])), None)
        device_id = registered['device_id'] if registered else serial
        reference = node_id + '/' + quote(device_id, safe='')
        declared = {'scope': 'device', 'binding': {'device_id': device_id, 'serial': serial}}
        queued = any(conflicts(declared, row['device_scope']) for row in queue.get('items', []))
        free = (not fleet.get('stale', True) and not queued and registered is not None
                and registered.get('status') == 'clear' and (scopes.get('global') or {}).get('status') == 'clear')
        result.setdefault(reference, {'ref': reference, 'node_id': node_id, 'node_name': name,
            'device_id': device_id, 'serial': serial, 'aliases': registered.get('aliases', []) if registered else [serial],
            'model': phone.get('model'), 'connected': connected, 'idle': free,
            'device_binding': ({'schema': 'phonefarm/device-binding@1', 'registry_revision': scopes['registry_revision'],
                                'device_id': device_id, 'serial': serial}
                               if registered else None),
            'sampled_at': fleet.get('sampled_at'), 'stale': fleet.get('stale', True), 'observation': phone})
    return list(result.values())


def catalog(jobs):
    result = phones(jobs.node_id, 'local', {'fleet': jobs.fleet.snapshot(), 'queue': jobs.dispatch.snapshot()}, True)
    for entry in jobs.directory.list():
        result.extend(phones(entry['node_id'], entry['name'], entry.get('snapshot') or {},
                             entry['connection']['status'] == 'ready'))
    return sorted(result, key=lambda item: item['ref'])


def select(jobs, request):
    spec = dict(request['spec'])
    node_id = spec.pop('execution_node_id', jobs.node_id)
    model, reference = spec.pop('device_model', None), spec.pop('device_ref', None)
    requirements = request['research_devices']
    if reference is None and model is None and len(requirements) == 1 and '/' in requirements[0]:
        reference = requirements[0]
    if model is None and reference is None:
        return node_id, {**request, 'spec': spec}, None
    candidates = catalog(jobs)
    selected = [phone for phone in candidates if
                (model is None or phone['model'] == model) and
                (reference is None or reference in (phone['ref'], phone['device_id'], phone['serial'], *phone['aliases'])) and
                ('execution_node_id' not in request['spec'] or phone['node_id'] == node_id)]
    if not selected and reference is not None and model is None and '/' in reference:
        # An explicit node/raw-serial keeps the existing unregistered-device path.
        target, serial = reference.split('/', 1)
        nodes = {jobs.node_id, *(entry['node_id'] for entry in jobs.directory.list())}
        if target in nodes and ('execution_node_id' not in request['spec'] or target == node_id):
            serial = unquote(serial)
            selected = [{'ref': reference, 'node_id': target, 'device_id': serial, 'serial': serial,
                         'aliases': [serial], 'model': None, 'idle': False}]
    if model is None and len(selected) > 1:
        raise ValueError('phone reference is ambiguous; choose one: ' + ', '.join(phone['ref'] for phone in selected))
    if model is not None:
        selected = [phone for phone in selected if phone['connected']]
    if not selected:
        raise ValueError('no matching phone is currently available; candidates: ' + ', '.join(
            phone['ref'] + ' ' + str(phone['model']) + (' (disconnected)' if not phone['connected'] else '') for phone in candidates))
    phone = min(selected, key=lambda item: (not item['idle'], item['ref']))
    argv = [phone['serial'] if arg == '@serial' else arg for arg in spec.get('argv', [])]
    actual = serial_from_argv(argv)
    if actual is not None and actual not in (phone['serial'], *phone['aliases']):
        raise ValueError('command serial differs from the selected phone; pass @serial as a complete argument')
    spec['argv'] = argv
    spec['resources'] = sorted(set(spec.get('resources', []) + ['device:' + phone['device_id']]))
    return phone['node_id'], {'spec': spec, 'research_devices': []}, phone
