"""Business intake and job sources; execution owns the only resource queue."""
import os
import re
import secrets


class JobArtifacts:
    """Local source adapter shared by result registration, inputs and job reads."""
    def __init__(self, executor):
        self.executor = executor

    def require_job(self, jid):
        if self.executor.dispatch.queue.get(jid):
            return
        os.close(self.executor._read_root.open_fd(jid, directory=True))

    def open_output(self, jid, path):
        return self.executor._read_root.open_file(jid + '/out/' + path)

    def metadata(self, jid):
        return self.executor._read_meta(jid)

    def completion_problem(self, jid):
        return self.executor.source_completion_problem(jid, self.executor.status(jid))

    def list(self, jid, query):
        from node_read import artifact_page
        return artifact_page(self.executor._read_root, jid, query)


class JobCenter:
    def __init__(self, local):
        self.local = local
        self.artifacts = JobArtifacts(local)

    def prepare_request(self, spec):
        """Resolve business references once, before selecting an execution source."""
        if not isinstance(spec, dict):
            raise ValueError('job spec must be an object')
        spec = dict(spec)
        actor_id = spec.get('actor_id')
        if actor_id is not None:
            if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 64:
                raise ValueError('actor_id 必须是 1-64 字符的非空字符串')
            spec['actor_id'] = actor_id.strip()
        research_id = spec.get('research_id')
        research_devices = []
        store = getattr(self.local, 'research', None)
        if research_id is not None:
            try:
                if store is None:
                    raise ValueError('this node keeps no research records')
                research_devices = store.get('research', research_id)['data'].get('device_requirements') or []
            except ValueError as error:
                raise ValueError(f'research_id must name a research on this node: {error}') from None
        direction_id = spec.get('direction_id')
        if direction_id is not None:
            if research_id is None:
                raise ValueError('direction_id requires research_id')
            try:
                if store is None:
                    raise ValueError('this node keeps no research records')
                if store.get('direction', direction_id)['research_id'] != research_id:
                    raise ValueError('direction_id must belong to research_id')
            except ValueError as error:
                raise ValueError(f'direction_id must name a direction on research {research_id}: {error}') from None
        return {'spec': spec, 'research_devices': list(research_devices)}

    def start(self, spec, body, request_id=None, *, admit=None):
        request = self.prepare_request(spec)
        return self.local.accept(request, body, request_id, job_id=secrets.token_hex(6), admit=admit)

    def job_ids(self):
        # The local source is already durable. Do not copy its queue into a second ledger.
        return {jid for jid in os.listdir(self.local.dir) if re.fullmatch(r'[0-9a-f]{12}', jid)} | {
            row['job_id'] for row in self.local.dispatch.queue.rows()}
