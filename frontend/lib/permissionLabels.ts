import type { AdminPermission } from "./types";

export const systemPermissionLabels: Record<string, string> = {
  "knowledge.manage": "管理知识库",
  "agent.view": "查看智能体",
  "agent.use": "使用智能体",
  "agent.create": "创建智能体",
  "agent.update": "编辑智能体",
  "agent.delete": "删除智能体",
  "agent.publish": "发布智能体",
  "skill.manage": "管理技能",
  "mcp.manage": "管理工具连接",
  "role.bind": "分配管理职责",
  "scope.manage": "管理部门结构",
  "user.read": "查看用户账号",
  "user.manage": "管理用户账号",
  "membership.read": "查看部门成员",
  "membership.manage": "管理部门成员",
  "role.read": "查看角色目录",
  "role.manage": "管理角色定义",
  "audit.read": "查看管理审计",
  "usage.read": "查看范围用量与成本",
};

export function permissionDisplayName(permission: AdminPermission): string {
  if (permission.display_name && (!permission.is_system || permission.display_name !== permission.key)) {
    return permission.display_name;
  }
  return systemPermissionLabels[permission.key] ?? permission.display_name ?? permission.key;
}

export function permissionKeyLabel(key: string): string {
  return systemPermissionLabels[key] ?? "扩展权限（详见技术标识）";
}
