"""Read-only authorization simulation for a proposed department move.

Compare grant unions, not share-row counts. No resource identity leaves this
module: a department administrator may move a branch without managing every
resource shared into it. The caller serializes committed moves on the root row.
"""
from collections import defaultdict

from sqlalchemy import select

from workmate_product.persistence.models import (
    Agent, AgentCapabilityBinding, CapabilityInstallation, CapabilityType,
    KnowledgeBase, ResourceShare, Scope, ScopeClosure, ScopeKind,
    Visibility,
)


class _AccessSnapshot:
    def __init__(self, scopes, ancestors, grants, capabilities, libraries):
        self.scopes = scopes
        self.ancestors = ancestors
        self.grants = grants
        self.capabilities = capabilities
        self.libraries = libraries

    def shared(self, kind, resource_id, target_id, *, branch=False, download=False):
        for grant in self.grants.get((kind, resource_id), ()):
            if download and not grant.allow_download:
                continue
            if not branch and grant.target_scope_id == target_id:
                return True
            if (grant.include_descendants
                    and grant.target_scope_id in self.ancestors.get(target_id, set())
                    and self.scopes[grant.target_scope_id].kind != ScopeKind.PERSONAL):
                return True
        return False

    def dependency(self, kind, resource_id, target_id, *, branch=False):
        resource = (self.capabilities if kind in ('skills', 'mcps') else self.libraries).get(resource_id)
        target = self.scopes.get(target_id)
        if resource is None or not resource.enabled or target is None:
            return False
        source = self.scopes.get(resource.scope_id)
        if source is None:
            return False
        if source.kind == ScopeKind.PERSONAL or target.kind == ScopeKind.PERSONAL:
            return False
        return self.shared(kind, resource_id, target_id, branch=branch)


def _resource_pairs(snapshot, department_ids, resources):
    result = {kind: set() for kind in ('agents', 'skills', 'mcps', 'knowledge_bases', 'knowledge_downloads')}
    for kind, ids in resources.items():
        for resource_id in ids:
            for scope_id in department_ids:
                if snapshot.shared(kind, resource_id, scope_id):
                    result[kind].add((scope_id, resource_id))
                if kind == 'knowledge_bases' and snapshot.shared(kind, resource_id, scope_id, download=True):
                    result['knowledge_downloads'].add((scope_id, resource_id))
    return result


def _lost_dependencies(before, after, agents, bindings, grants):
    lost = set()
    for agent in agents.values():
        dependencies = [('mcps' if before.capabilities.get(resource_id) is not None and before.capabilities[resource_id].capability_type == CapabilityType.MCP else 'skills', resource_id)
                        for resource_id in bindings.get(agent.id, ())]
        dependencies += [('knowledge_bases', resource_id)
                         for resource_id in (agent.knowledge_config or {}).get('knowledge_base_ids', [])]
        contexts = [(agent.scope_id, False)]
        if agent.visibility == Visibility.PUBLIC:
            contexts += [(grant.target_scope_id, grant.include_descendants)
                         for grant in grants.get(('agents', agent.id), ())]
        for kind, resource_id in dependencies:
            for target_id, branch in contexts:
                if (before.dependency(kind, resource_id, target_id, branch=branch)
                        and not after.dependency(kind, resource_id, target_id, branch=branch)):
                    lost.add((agent.id, kind, resource_id, target_id, branch))
    return len(lost)


async def move_access_impact(db, descendants, old_ancestors, new_ancestors):
    scopes = {scope.id: scope for scope in (await db.scalars(select(Scope))).all()}
    ancestors = defaultdict(set)
    for ancestor, descendant in (await db.execute(select(ScopeClosure.ancestor_id, ScopeClosure.descendant_id))).all():
        ancestors[descendant].add(ancestor)
    proposed = {sid: ((values - old_ancestors) | new_ancestors if sid in descendants else set(values))
                for sid, values in ancestors.items()}
    agents = {item.id: item for item in (await db.scalars(select(Agent).join(
        Scope, Scope.id == Agent.scope_id).where(Agent.deleted_at.is_(None),
                                              Scope.kind != ScopeKind.PERSONAL))).all()}
    capabilities = {item.id: item for item in (await db.scalars(select(CapabilityInstallation))).all()}
    libraries = {item.id: item for item in (await db.scalars(select(KnowledgeBase))).all()}
    grants = defaultdict(list)
    for grant in (await db.scalars(select(ResourceShare))).all():
        for kind, resource_id in (('agents', grant.agent_id), ('mcps' if grant.capability_id in capabilities and capabilities[grant.capability_id].capability_type == CapabilityType.MCP else 'skills', grant.capability_id),
                                  ('knowledge_bases', grant.knowledge_base_id)):
            if resource_id:
                grants[kind, resource_id].append(grant)
    before = _AccessSnapshot(scopes, ancestors, grants, capabilities, libraries)
    after = _AccessSnapshot(scopes, proposed, grants, capabilities, libraries)
    resources = {
        'agents': {a.id for a in agents.values() if a.visibility == Visibility.PUBLIC
                   and scopes[a.scope_id].kind != ScopeKind.PERSONAL},
        'skills': {c.id for c in capabilities.values() if c.capability_type == CapabilityType.SKILL
                   and scopes[c.scope_id].kind != ScopeKind.PERSONAL},
        'mcps': {c.id for c in capabilities.values() if c.capability_type == CapabilityType.MCP
                 and scopes[c.scope_id].kind != ScopeKind.PERSONAL},
        'knowledge_bases': {k.id for k in libraries.values() if scopes[k.scope_id].kind != ScopeKind.PERSONAL},
    }
    # Only inherited grants on changed ancestors can alter a moved department's
    # resource union. Still evaluate every grant for each candidate, so an
    # independent direct/ancestor grant cancels a nominal loss.
    changed_ancestors = old_ancestors ^ new_ancestors
    for kind, ids in resources.items():
        resources[kind] = {rid for rid in ids if any(
            grant.include_descendants and grant.target_scope_id in changed_ancestors
            for grant in grants.get((kind, rid), ()))}
    departments = {sid for sid in descendants if scopes[sid].kind == ScopeKind.DEPARTMENT}
    old_pairs = _resource_pairs(before, departments, resources)
    new_pairs = _resource_pairs(after, departments, resources)
    changes = {kind: {'gained': len(new_pairs[kind] - old_pairs[kind]),
                      'lost': len(old_pairs[kind] - new_pairs[kind])} for kind in old_pairs}
    bindings = defaultdict(set)
    for agent_id, capability_id in (await db.execute(select(
            AgentCapabilityBinding.agent_id, AgentCapabilityBinding.capability_installation_id))).all():
        bindings[agent_id].add(capability_id)
    return {'shared_resource_changes': changes,
            'capability_blockers': _lost_dependencies(before, after, agents, bindings, grants)}
