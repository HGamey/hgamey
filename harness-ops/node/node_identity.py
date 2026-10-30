"""The service and its local executor share the existing persistent node ID."""
import os
import re
import uuid


def load_identity(data_root):
    identity_dir = os.path.join(data_root, '_node')
    os.makedirs(identity_dir, exist_ok=True)
    identity_path = os.path.join(identity_dir, 'id')
    temporary = identity_path + '.' + uuid.uuid4().hex
    try:
        with open(temporary, 'x', encoding='utf-8') as stream:
            stream.write(uuid.uuid4().hex + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, identity_path)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    with open(identity_path, encoding='utf-8') as stream:
        node_id = stream.read().strip()
    if not re.fullmatch(r'[0-9a-f]{32}', node_id):
        raise ValueError('invalid persistent node identity; preserve and repair _node/id')
    return node_id
