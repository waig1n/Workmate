"""Materialize Product-owned Start attachments without Web/config dependencies.

The assembler calls this once for a fresh Start, never for Resume. All source
files are pinned and checked before the first Task-backend write. A copy error
or cancellation must abort preparation; partial workspace files are not proof
of admission or successful input delivery.
"""
from contextlib import ExitStack,aclosing
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import posixpath
import stat


MAX_ATTACHMENT_FILES = 10
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # Per file, matching Product upload limits.
ATTACHMENT_CHUNK_BYTES = 64 * 1024


class AttachmentMaterializationError(ValueError):
    """Task input is unavailable or inconsistent; no successful preparation."""


def _component(value):
    if (not isinstance(value,str) or not value or value in {'.','..'}
            or any(c in '/\\' or ord(c)<32 for c in value)):
        raise AttachmentMaterializationError('Attachment identity must be a single safe path component')
    return value


def _open_dir(name, stack, *, parent=None):
    fd = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
    stack.callback(os.close,fd)
    return fd


def _source_directory(root, conversation, task, stack):
    # Walk the configured root too: O_NOFOLLOW only on its last component
    # would still permit an intermediate directory symlink escape.
    absolute = Path(os.path.abspath(os.fspath(root)))
    fd = _open_dir(absolute.anchor,stack)
    for part in absolute.parts[1:]:
        fd = _open_dir(part,stack,parent=fd)
    for part in ('.inputs',conversation,task):
        fd = _open_dir(part,stack,parent=fd)
    return fd


def _metadata(messages, task_id):
    outputs,attachments,used = [],[],set()
    for message in messages:
        if not isinstance(message.content,dict):
            raise AttachmentMaterializationError('Product input content must be an object')
        content = deepcopy(message.content)
        if not isinstance(content.get('text',''),str):
            raise AttachmentMaterializationError('Product input text must be a string')
        items = content.get('attachments') or []
        if not isinstance(items,list):
            raise AttachmentMaterializationError('Attachments must be a list')
        for item in items:
            if not isinstance(item,dict):
                raise AttachmentMaterializationError('Attachment metadata must be an object')
            name = _component(item.get('name'))
            if item.get('path') != f'input/{task_id}/{name}' or name.casefold() in used:
                raise AttachmentMaterializationError('Attachment path is foreign, duplicated or noncanonical')
            size = item.get('size')
            if type(size) is not int or not 0 <= size <= MAX_ATTACHMENT_BYTES:
                raise AttachmentMaterializationError('Attachment size exceeds the Product limit or is invalid')
            if not isinstance(item.get('mime_type'),str) or not item['mime_type'].strip():
                raise AttachmentMaterializationError('Attachment MIME metadata is missing')
            used.add(name.casefold())
            attachments.append(item)
        if items:
            paths = '\n'.join(f'- {item["path"]}' for item in items)
            content['text'] = (f'{content.get("text", "")}\n\n'
                '[平台附件]\n以下文件属于本任务，已放入工作区：\n'
                f'{paths}\n请按需使用 Read 或其他工作区工具读取。').strip()
        outputs.append(replace(message,content=content))
    if len(attachments)>MAX_ATTACHMENT_FILES:
        raise AttachmentMaterializationError('A Task may have at most ten attachments')
    return tuple(outputs),attachments


async def materialize_start_attachments(context, binding, *, artifact_storage_dir, object_store=None):
    """Return copies of original Product messages, with the existing attachment hint.

    No attachments means no source directory is required. The binding's backend
    owns process/resource fencing; this function never substitutes a host-local
    destination write or retries a cancelled/failed transfer.
    """
    conversation,task = _component(context.conversation_id),_component(context.task_id)
    if binding.owner.task_id != task or binding.owner.session_id != context.session_id:
        raise AttachmentMaterializationError('Attachment Task/Session differs from execution binding')
    messages,attachments = _metadata(context.input_messages,task)
    if not attachments:
        return messages
    if object_store is not None:
        await _materialize_objects(object_store,conversation,task,attachments,binding)
        return messages
    if not artifact_storage_dir:
        raise AttachmentMaterializationError('Attachment storage directory is required for file inputs')
    workdir = binding.workspace.workdir
    if not isinstance(workdir,str) or not workdir.startswith('/') or posixpath.normpath(workdir) != workdir:
        raise AttachmentMaterializationError('Task workspace must have a canonical absolute workdir')
    try:
        with ExitStack() as stack:
            parent = _source_directory(artifact_storage_dir,conversation,task,stack)
            sources = []
            for item in attachments:
                fd = os.open(item['name'],os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=parent)
                stack.callback(os.close,fd)
                initial = os.fstat(fd)
                if not stat.S_ISREG(initial.st_mode) or initial.st_size != item['size']:
                    raise AttachmentMaterializationError('Attachment file type or actual size differs from metadata')
                sources.append((item,fd,initial))
            for item,fd,initial in sources:
                consumed = False
                async def chunks():
                    nonlocal consumed
                    total = 0
                    while chunk := os.read(fd,ATTACHMENT_CHUNK_BYTES):
                        total += len(chunk)
                        if total > item['size'] or total > MAX_ATTACHMENT_BYTES:
                            raise AttachmentMaterializationError('Attachment grew while being copied')
                        yield chunk
                    final = os.fstat(fd)
                    if (total != item['size'] or (final.st_size,final.st_mtime_ns,final.st_ctime_ns)
                            != (initial.st_size,initial.st_mtime_ns,initial.st_ctime_ns)):
                        raise AttachmentMaterializationError('Attachment changed while being copied')
                    consumed = True
                async with aclosing(chunks()) as stream:
                    await binding.backend.write_stream(posixpath.join(workdir,item['path']),stream)
                if not consumed:
                    raise AttachmentMaterializationError('Task backend did not consume the complete attachment')
    except OSError as error:
        raise AttachmentMaterializationError('Task attachment source or transfer is unavailable') from error
    return messages


async def _materialize_objects(store, conversation, task, attachments, binding):
    # Snapshot verification finishes before any untrusted backend receives bytes.
    from workmate_runtime.delivery.coordinator import _joined_io
    workdir = binding.workspace.workdir
    if not isinstance(workdir,str) or not workdir.startswith('/') or posixpath.normpath(workdir)!=workdir:
        raise AttachmentMaterializationError('Task workspace must have a canonical absolute workdir')
    with ExitStack() as stack:
        sources = []
        for item in attachments:
            key = f'.inputs/{conversation}/{task}/{item["name"]}'
            # Metadata cannot redirect a task to another conversation's input.
            if item.get('storage_key',key) != key:
                raise AttachmentMaterializationError('Attachment object belongs to another Task')
            # Joined cancellation needs cleanup inside the worker thread too.
            holder = []
            def acquire():
                snapshot = store.snapshot(key,item.get('sha256'),item['size'])
                holder.append(snapshot)
                return snapshot
            try:
                snapshot = await _joined_io(acquire)
            except BaseException:
                for snapshot in holder:
                    snapshot.close()
                raise
            stack.callback(snapshot.close)
            source = stack.enter_context(snapshot.path.open('rb'))
            sources.append((item,source))
        for item,source in sources:
            consumed = False
            async def chunks():
                nonlocal consumed
                while chunk := source.read(ATTACHMENT_CHUNK_BYTES):
                    yield chunk
                consumed = True
            async with aclosing(chunks()) as stream:
                await binding.backend.write_stream(posixpath.join(workdir,item['path']),stream)
            if not consumed:
                raise AttachmentMaterializationError('Task backend did not consume the complete attachment')
