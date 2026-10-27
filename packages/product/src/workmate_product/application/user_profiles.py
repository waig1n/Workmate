"""Basic user information; never an external authentication assertion."""
from pydantic import BaseModel, ConfigDict, Field
from ..persistence.models import Scope
from ..persistence.user_profile_repository import UserProfileRepository
from .platform_types import PlatformToolError

USER_PROFILE_TOOLS = frozenset({'get_current_user', 'list_scope_users'})


class ListScopeUsersInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scope_id: str = Field(min_length=1, max_length=128)
    include_descendants: bool = False
    q: str | None = Field(default=None, max_length=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=100)


def _profile(user, departments):
    return {'id': user.id, 'display_name': user.display_name, 'email': user.email,
            'departments': departments}


async def readable_scopes(db, access, user):
    scopes = await UserProfileRepository(db).shared_scopes()
    permissions = await access.permissions_for_scopes(user.id, [scope.id for scope in scopes])
    global_reader = any(scope.kind == 'organization' and 'user.read' in permissions[scope.id]
                        for scope in scopes)
    allowed = [scope for scope in scopes if global_reader or 'membership.read' in permissions[scope.id]]
    return allowed


async def get_current_user(db, access, user):
    repository = UserProfileRepository(db)
    departments = await repository.departments([user.id])
    scopes = await readable_scopes(db, access, user)
    return {**_profile(user, departments[user.id]),
            'readable_scopes': [{'id': scope.id, 'name': scope.name, 'kind': scope.kind}
                                for scope in scopes]}


async def list_scope_users(db, access, user, args):
    scope = await db.get(Scope, args.scope_id)
    if scope is None:
        raise PlatformToolError(404, 'Scope not found')
    repository = UserProfileRepository(db)
    if scope.kind == 'personal':
        if scope.owner_user_id != user.id:
            raise PlatformToolError(403, 'Cannot read another personal scope')
        scope_ids, department_ids, owner = {scope.id}, None, user.id
    else:
        allowed = {item.id for item in await readable_scopes(db, access, user)}
        if scope.id not in allowed:
            raise PlatformToolError(403, 'User directory read permission required in this scope')
        requested = await repository.descendants(scope.id) if args.include_descendants else {scope.id}
        scope_ids, department_ids, owner = requested & allowed, requested & allowed, None
    users, cursor = await repository.page(scope_ids, q=args.q, cursor=args.cursor, limit=args.limit, user_id=owner)
    departments = await repository.departments([item.id for item in users], department_ids)
    return {'items': [_profile(item, departments[item.id]) for item in users], 'next_cursor': cursor}
