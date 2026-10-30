"""Explicit baseline pairs over the center's single result index."""
import json
import time

from node_results import ResultError, object_fields
from node_result_compare import MAX_SPEC, validate_spec, compare, build_rows

SCHEMA = 'phonefarm/result-cross-compare@1'
MAX_RESPONSE = 6 << 20


def local_spec(spec):
    return {'schema': 'phonefarm/result-compare@1', 'node_id': spec['node_id'],
            'columns': [{key: column[key] for key in ('id', 'label', 'result_id')} for column in spec['columns']],
            'rows': spec['rows'], **({'context': spec['context']} if 'context' in spec else {})}


def validate_cross_spec(spec, node_id):
    try:
        object_fields(spec, {'schema', 'node_id', 'columns', 'rows', 'context'})
        if spec.get('schema') != SCHEMA:
            raise ValueError('unsupported cross-node comparison schema')
        columns = spec.get('columns')
        if not isinstance(columns, list):
            raise ValueError('comparison columns must be an array')
        for column in columns:
            object_fields(column, {'id', 'label', 'result_id', 'baseline_column_id'})
            if 'baseline_column_id' not in column:
                raise ValueError('each column requires an explicit baseline_column_id or null')
        validate_spec(local_spec(spec), node_id)
        ids = {column['id'] for column in columns}
        for column in columns:
            baseline = column['baseline_column_id']
            if baseline is not None and (not isinstance(baseline, str) or baseline not in ids):
                raise ValueError('baseline_column_id must name a selected column or be null')
    except ResultError as error:
        raise ResultError(error.status, 'result_cross_compare_invalid', str(error)) from None
    except (ValueError, TypeError, KeyError) as error:
        raise ResultError(400, 'result_cross_compare_invalid', str(error)) from None
    return spec


def cross_compare(results, node_id, spec):
    spec = validate_cross_spec(spec, node_id)
    value = compare(results, node_id, local_spec(spec))
    for column, requested in zip(value['columns'], spec['columns']):
        record = column['result']
        source_id = (record['provenance'].get('execution_node_id', node_id) if record else None)
        column.update(node_id=source_id, baseline_column_id=requested['baseline_column_id'],
                      source_status='ok' if record else 'unavailable', read_at=time.time() if record else None)
    value.pop('baseline_column_id')
    value.update(spec=spec, rows=build_rows(value['columns'], spec['rows'],
                 {column['id']: column['baseline_column_id'] for column in spec['columns']}))
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE:
        raise ResultError(413, 'result_cross_compare_too_large', 'comparison exceeds 6 MiB; select fewer columns or metric rows')
    return value


def cross_compare_query(results, node_id, query):
    if (set(query) - {'spec', 'expected_node_id'} or 'spec' not in query
            or any(len(values) != 1 for values in query.values())):
        raise ResultError(400, 'result_cross_compare_invalid', 'comparison requires exactly one spec')
    raw = query['spec'][0]
    if len(raw.encode()) > MAX_SPEC:
        raise ResultError(400, 'result_cross_compare_invalid', 'comparison spec exceeds 16 KiB')
    def invalid_constant(_):
        raise ValueError('non-finite JSON constant')
    try:
        spec = json.loads(raw, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        raise ResultError(400, 'result_cross_compare_invalid', 'comparison spec is not valid JSON') from None
    return cross_compare(results, node_id, spec)
