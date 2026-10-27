"""Durable user/Agent placement using the existing memory namespace ledger."""
from sqlalchemy import text


class NodePlacementRepository:
    def __init__(self, db):
        self.db = db

    async def nodes(self, user_id, agent_id):
        rows = await self.db.execute(text('''SELECT storage_node_id FROM runtime_memory_namespaces
            WHERE user_id=:user AND agent_id=:agent UNION
            SELECT s.storage_node_id FROM runtime_sessions s JOIN conversations c ON c.id=s.conversation_id
            WHERE c.user_id=:user AND c.agent_id=:agent
            AND NOT EXISTS (SELECT 1 FROM recovery_forks f WHERE f.target_session_id=s.id)'''), {'user': user_id, 'agent': agent_id})
        return set(rows.scalars())

    async def bind(self, user_id, agent_id, node):
        args = {'user': user_id, 'agent': agent_id, 'node': node}
        await self.db.execute(text('''INSERT INTO runtime_memory_namespaces(user_id,agent_id,storage_node_id)
            VALUES (:user,:agent,:node) ON CONFLICT(user_id,agent_id) DO NOTHING'''), args)
        return await self.db.scalar(text('''SELECT storage_node_id FROM runtime_memory_namespaces
            WHERE user_id=:user AND agent_id=:agent'''), args)
