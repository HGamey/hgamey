"""An actual outbound connection between two isolated HTTP fixtures."""
import threading

from node_connector import LocalHTTP, NodeConnector


def connect(directory, center_url, name, worker_url, worker_token, worker_id):
    joined = directory.join({'invite': directory.invite()['code'], 'name': name, 'node_id': worker_id})
    connector = NodeConnector({'url': center_url, 'center_id': joined['center_id'], 'token': joined['token']},
                              LocalHTTP(worker_url, worker_token, worker_id))
    reported = threading.Event()
    original = connector._json

    def report(path, value):
        result = original(path, value)
        if path == '/node-connect/report':
            reported.set()
        return result

    connector._json = report
    connector.start()
    if not reported.wait(5):
        connector.close()
        raise AssertionError('node did not upload its first snapshot')
    return connector
