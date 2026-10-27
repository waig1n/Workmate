"""Sticky placement; unavailable owners never cause implicit relocation."""
import hashlib
from ..persistence.repositories.node_placement import NodePlacementRepository


class NodePlacementUnavailable(RuntimeError):
    pass


async def select_storage_node(db, user_id, agent_id, directory, *, bind=False):
    repository = NodePlacementRepository(db)
    nodes = await repository.nodes(user_id, agent_id)
    if len(nodes) > 1:
        raise NodePlacementUnavailable('Existing conversations have conflicting storage nodes; operator review is required')
    if nodes:
        node = next(iter(nodes))
        if not directory.available(node):
            raise NodePlacementUnavailable('Conversation storage node is unavailable; retry after it recovers')
    else:
        candidates = directory.candidates()
        if not candidates:
            raise NodePlacementUnavailable('No available Runtime storage node')
        # Stable rendezvous selection distributes new namespaces without a scheduler.
        node = max(candidates, key=lambda value: hashlib.sha256(
            f'{user_id}\0{agent_id}\0{value}'.encode()).digest())
    if bind:
        actual = await repository.bind(user_id, agent_id, node)
        if actual != node:
            raise NodePlacementUnavailable('Storage node binding changed; retry admission')
    return node
