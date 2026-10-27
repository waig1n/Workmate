# -*- coding: utf-8 -*-
"""Built-in permission keys (RBAC design §5).

These literal keys are seeded into the ``permissions`` table and used
by RoleBinding checks; keep them in sync with the alembic seed.
"""

AGENT_VIEW = "agent.view"
AGENT_USE = "agent.use"
AGENT_CREATE = "agent.create"
AGENT_UPDATE = "agent.update"
AGENT_DELETE = "agent.delete"
AGENT_PUBLISH = "agent.publish"
SKILL_MANAGE = "skill.manage"
MCP_MANAGE = "mcp.manage"
CHANNEL_MANAGE = "channel.manage"
ROLE_BIND = "role.bind"
SCOPE_MANAGE = "scope.manage"
USER_READ = "user.read"
USER_MANAGE = "user.manage"
MEMBERSHIP_READ = "membership.read"
MEMBERSHIP_MANAGE = "membership.manage"
ROLE_READ = "role.read"
ROLE_MANAGE = "role.manage"
AUDIT_READ = "audit.read"
USAGE_READ = "usage.read"
KNOWLEDGE_MANAGE = "knowledge.manage"

SYSTEM_PERMISSION_LABELS = {
    KNOWLEDGE_MANAGE: "管理知识库",
    AGENT_VIEW: "查看智能体",
    AGENT_USE: "使用智能体",
    AGENT_CREATE: "创建智能体",
    AGENT_UPDATE: "编辑智能体",
    AGENT_DELETE: "删除智能体",
    AGENT_PUBLISH: "发布智能体",
    SKILL_MANAGE: "管理技能",
    MCP_MANAGE: "管理工具连接",
    CHANNEL_MANAGE: "管理消息渠道",
    ROLE_BIND: "分配管理职责",
    SCOPE_MANAGE: "管理部门结构",
    USER_READ: "查看用户账号",
    USER_MANAGE: "管理用户账号",
    MEMBERSHIP_READ: "查看部门成员",
    MEMBERSHIP_MANAGE: "管理部门成员",
    ROLE_READ: "查看角色目录",
    ROLE_MANAGE: "管理角色定义",
    AUDIT_READ: "查看管理审计",
    USAGE_READ: "查看范围用量与成本",
}

ROOT_ONLY_PERMISSIONS = frozenset({USER_READ, USER_MANAGE, ROLE_READ, ROLE_MANAGE, CHANNEL_MANAGE})
ROOT_ADMIN_PERMISSIONS = frozenset({
    USER_READ, USER_MANAGE, ROLE_READ, ROLE_MANAGE, ROLE_BIND,
    SCOPE_MANAGE, MEMBERSHIP_READ, MEMBERSHIP_MANAGE,
})

ALL_PERMISSIONS = tuple(SYSTEM_PERMISSION_LABELS)

ROLE_ADMIN = "admin"

# Ordinary membership is account identity, not a role grant.
ROLE_GRANTS = {
    ROLE_ADMIN: ALL_PERMISSIONS,
}

# Templates are copied into custom roles by administrators, never automatic grants.
ROLE_TEMPLATES = (
    {"key": "account_manager", "display_name": "账号管理员", "permission_keys": [USER_READ, USER_MANAGE], "delegatable": False},
    {"key": "department_manager", "display_name": "部门成员管理员", "permission_keys": [MEMBERSHIP_READ, MEMBERSHIP_MANAGE, SCOPE_MANAGE], "delegatable": True},
    {"key": "agent_manager", "display_name": "Agent 管理员", "permission_keys": [AGENT_CREATE, AGENT_UPDATE, AGENT_DELETE, AGENT_PUBLISH], "delegatable": True},
    {"key": "capability_manager", "display_name": "能力管理员", "permission_keys": [SKILL_MANAGE, MCP_MANAGE, KNOWLEDGE_MANAGE], "delegatable": True},
    {"key": "auditor", "display_name": "审计员", "permission_keys": [AUDIT_READ], "delegatable": True},
)
