// Stable backend error-code -> user-facing Chinese message mapping
// (user management plan §8.2: the frontend must not depend on natural
// language detail strings for flow decisions).

const CODE_MESSAGES: Record<string, string> = {
  organization_name_required: "请填写组织名称。",
  organization_already_initialized: "企业工作空间已创建，请登录或联系管理员开通账号。",
  organization_not_found: "组织不存在或当前无权访问。",
  department_roles_require_authorization: "调岗涉及原部门管理职责，请由有职责授权权限的管理员处理。",
  organization_membership_managed: "企业成员身份由账号自动维护，离职请禁用账号。",
  role_not_assignable: "该管理职责超出你的授权范围，请联系组织管理员。",
  last_root_admin: "该操作会移除最后一个活跃的根管理员，请先创建或恢复另一个管理员。",
  email_already_registered: "该邮箱已被注册。",
  root_admin_required: "需要根管理员权限。",
  role_bind_required: "没有分配角色的权限。",
  scope_manage_required: "没有管理该组织或部门的权限。",
  permission_required: "没有执行此管理操作的权限，请联系组织管理员。",
  version_conflict: "其他管理员已修改此对象。请重新加载最新内容，对照差异后再保存。",
  delegation_forbidden: "该角色超出你可以分配的权限或部门范围。",
  role_not_delegatable: "该角色未开放部门委派，请联系组织管理员。",
  user_not_found: "用户不存在或已被删除。",
  scope_not_found: "部门不存在或不可访问。",
  role_not_found: "角色不存在。",
  permission_not_found: "权限项不存在。",
  scope_name_taken: "同级部门已存在相同名称。",
  scope_cycle: "不能将部门移动到自身或下级部门之下。",
  root_scope_immutable: "组织根节点不可执行该操作。",
  personal_scope_forbidden: "个人空间由账号自动维护，不能在部门管理中修改。",
  scope_not_empty: "部门仍有关联成员或资源，无法删除。",
  scope_capability_conflict: "移动后部分 Agent 将无法使用已绑定的能力，请先调整能力绑定。",
  system_role: "系统角色受到保护，可复制后创建自定义角色。",
  system_permission: "系统权限项不可删除。",
  role_in_use: "该角色仍被使用，请先处理相关用户的角色授权。",
  permission_in_use: "该权限仍被角色使用，请先从对应角色中移除。",
  reserved_role_key: "普通成员是企业账号的默认身份，无需创建或分配此角色。",
  role_key_taken: "角色标识已被占用。",
  permission_key_taken: "权限标识已被占用。",
  duplicate_role_binding: "同一范围不能重复分配相同角色。",
  invalid_cursor: "分页游标无效，请刷新列表。",
  invalid_current_password: "当前密码不正确。",
  password_change_required: "请先修改密码后再继续。",
  user_already_disabled: "该账号已处于禁用状态。",
  runtime_unreachable: "Runtime 暂不可达，请稍后重试。",
};

/** Map an ApiError (or any thrown value) to a Chinese message; falls
 * back to the raw error message when no stable code matches. */
export function adminErrorMessage(reason: unknown): string {
  const error = reason as { code?: string | null; message?: string };
  if (error?.code && CODE_MESSAGES[error.code]) {
    return CODE_MESSAGES[error.code];
  }
  return error?.message ?? String(reason);
}
