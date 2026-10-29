"""Per-Worker subscriptions; omitted settings preserve legacy all-queue behaviour."""
QUEUES = ('workflows', 'workflows_data', 'scenarios')
CAPABILITY = {"schema": 1}


def execution_queues(host):
    value = host.get('execution_queues', list(QUEUES))
    if (not isinstance(value, list) or not value
            or any(not isinstance(q, str) or q not in QUEUES for q in value)
            or len(set(value)) != len(value)):
        raise ValueError('Worker execution_queues: select at least one of Model-driven, Data-driven, Forecast; duplicates are not allowed')
    return [q for q in QUEUES if q in value]


def subscriptions(config):
    return {role: execution_queues(host) for role, host in config['hosts'].items() if role.startswith('worker')}


def validate_release_queues(config, release):
    capability = getattr(release, 'doc', {}).get('management', {}).get('worker_queue_selection')
    if capability is not None and (capability != CAPABILITY or type(capability.get('schema')) is not int):
        raise ValueError('Unsupported Worker queue selection contract')
    if capability is None and any(queues != list(QUEUES) for queues in subscriptions(config).values()):
        raise ValueError('Selected Worker queues require a compatible Worker package with queue selection support')
