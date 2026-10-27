"""Independent bounded conversation log with explicit incarnation and replay proof.

Only a trusted PG coordinator initializes and confirms LogRef. Every other call
requires that confirmed reference. Automatic Redis retries are prohibited: an
ambiguous initialize must never replay the same incarnation after total key loss.
"""
import asyncio
from dataclasses import dataclass
import hashlib
from uuid import uuid4

from workmate_contracts.realtime import RealtimeCursor, RealtimeEnvelope
from ..coordination.command_queue import _identifier, _text


class LogLost(RuntimeError):
    """The confirmed log or its integrity proof is unavailable."""


class ReplayGap(RuntimeError):
    """The requested position no longer has provably contiguous coverage."""


class FutureCursor(ValueError):
    """The requested position is ahead of this log."""


@dataclass(frozen=True)
class LogRef:
    conversation_id: str
    generation: str
    incarnation: str

    def __post_init__(self):
        RealtimeCursor(conversation_id=self.conversation_id,generation=self.generation,stream_id='0-0')
        RealtimeCursor(conversation_id=self.conversation_id,generation=self.incarnation,stream_id='0-0')


@dataclass(frozen=True)
class LogWindow:
    floor: RealtimeCursor
    first: RealtimeCursor | None
    last: RealtimeCursor
    payload_bytes: int
    floor_ordinal: int
    last_ordinal: int


@dataclass(frozen=True)
class LogEntry:
    cursor: RealtimeCursor
    envelope: RealtimeEnvelope


@dataclass(frozen=True)
class LogPage:
    entries: tuple[LogEntry, ...]
    window: LogWindow


@dataclass(frozen=True)
class AppendResult:
    cursor: RealtimeCursor
    window: LogWindow


_SCRIPT = r"""
local function less(a,b)
  local am,as=string.match(a,'^(%d+)%-(%d+)$')
  local bm,bs=string.match(b,'^(%d+)%-(%d+)$')
  if not am or not bm then return false end
  if #am~=#bm then return #am<#bm end
  if am~=bm then return am<bm end
  if #as~=#bs then return #as<#bs end
  return as<bs
end
local function fields(raw)
  local out={}
  for i=1,#raw,2 do out[raw[i]]=raw[i+1] end
  return out
end
local op,conversation,generation,nonce=ARGV[1],ARGV[2],ARGV[3],ARGV[4]
local retention,budget,ttl=tonumber(ARGV[5]),tonumber(ARGV[6]),tonumber(ARGV[7])
local meta_exists=redis.call('EXISTS',KEYS[1])
local stream_exists=redis.call('EXISTS',KEYS[2])
if op=='initialize' and meta_exists==0 and stream_exists==0 then
  redis.call('XADD',KEYS[2],'0-1','marker',nonce,'ordinal','0','bytes','0')
  redis.call('HSET',KEYS[1],'conversation',conversation,'generation',generation,'incarnation',nonce,
    'floor','0-0','tail','0-1','bytes','0','count','1','ordinal','0','floor_ordinal','0')
elseif meta_exists~=1 or stream_exists~=1 then return {'lost'} end
if redis.call('TYPE',KEYS[1]).ok~='hash' or redis.call('TYPE',KEYS[2]).ok~='stream' then return {'lost'} end
local m=fields(redis.call('HGETALL',KEYS[1]))
if m.conversation~=conversation or m.generation~=generation or not m.incarnation then return {'lost'} end
if op~='initialize' and m.incarnation~=nonce then return {'lost'} end
local bytes,count,ordinal,floorordinal=tonumber(m.bytes),tonumber(m.count),tonumber(m.ordinal),tonumber(m.floor_ordinal)
if not bytes or not count or not ordinal or not floorordinal or bytes<0 or count<0 or ordinal<floorordinal
  or not m.floor or not m.tail then return {'lost'} end
local info=fields(redis.call('XINFO','STREAM',KEYS[2]))
if info['last-generated-id']~=m.tail or redis.call('XLEN',KEYS[2])~=count then return {'lost'} end
local function snapshot(records,appended)
  local first=redis.call('XRANGE',KEYS[2],'-','+','COUNT',2)
  local firstid=''
  for _,row in ipairs(first) do if fields(row[2]).payload then firstid=row[1];break end end
  redis.call('HSET',KEYS[1],'floor',m.floor,'tail',m.tail,'bytes',string.format('%.0f',bytes),
    'count',string.format('%.0f',count),'ordinal',string.format('%.0f',ordinal),
    'floor_ordinal',string.format('%.0f',floorordinal))
  redis.call('PEXPIRE',KEYS[1],ttl)
  redis.call('PEXPIRE',KEYS[2],ttl)
  return {'ok',m.incarnation,m.floor,firstid,m.tail,string.format('%.0f',bytes),
    string.format('%.0f',floorordinal),string.format('%.0f',ordinal),records or {},appended or ''}
end
if op=='initialize' then return snapshot() end
local incoming=0
if op=='append' then
  incoming=string.len(ARGV[8])
  if incoming>budget or ordinal>=9007199254740991 then return {'oversized'} end
end
-- Reserve incoming bytes before XADD. A maintenance_pending response therefore
-- proves this append has not executed; only that response permits continuation.
local trimmed=0
local now=redis.call('TIME')
local cutoff=string.format('%.0f',math.max(0,tonumber(now[1])*1000+math.floor(tonumber(now[2])/1000)-retention))..'-0'
while true do
  local rows=redis.call('XRANGE',KEYS[2],'-','+','COUNT',1)
  if #rows==0 then break end
  local row=rows[1];local f=fields(row[2])
  local rowbytes=tonumber(f.bytes);local rowordinal=tonumber(f.ordinal)
  if not rowbytes or not rowordinal or (not f.marker and not f.payload)
    or (f.payload and (rowbytes~=string.len(f.payload) or rowordinal~=floorordinal+1))
    or (f.marker and (f.marker~=m.incarnation or rowordinal~=0 or rowbytes~=0)) then return {'lost'} end
  if not less(row[1],cutoff) and bytes+incoming<=budget then break end
  -- At most 64 deletions per invocation, including the initialization sentinel.
  -- Commit the exact intermediate floor/ordinal/count before yielding to Redis.
  if trimmed>=64 then
    local progress=snapshot()
    progress[1]='maintenance_pending'
    return progress
  end
  redis.call('XDEL',KEYS[2],row[1]);bytes=bytes-rowbytes;count=count-1
  if f.payload then m.floor=row[1];floorordinal=rowordinal end
  trimmed=trimmed+1
end
if op=='append' then
  ordinal=ordinal+1
  m.tail=redis.call('XADD',KEYS[2],'*','payload',ARGV[8],'ordinal',string.format('%.0f',ordinal),
    'bytes',tostring(incoming))
  bytes=bytes+incoming;count=count+1
end
if op=='read' then
  local after=ARGV[8]
  if less(after,m.floor) then snapshot();return {'gap'} end
  if less(m.tail,after) then snapshot();return {'future'} end
  local previous=floorordinal
  if after~=m.floor then
    local found=redis.call('XRANGE',KEYS[2],after,after,'COUNT',1)
    if #found~=1 then snapshot();return {'gap'} end
    previous=tonumber(fields(found[1][2]).ordinal)
    if not previous then return {'lost'} end
  end
  local rows=redis.call('XRANGE',KEYS[2],'('..after,'+','COUNT',tonumber(ARGV[9])+1)
  local result={}
  for _,row in ipairs(rows) do
    local f=fields(row[2])
    if f.payload then
      if tonumber(f.ordinal)~=previous+1 or tonumber(f.bytes)~=string.len(f.payload) then return {'lost'} end
      previous=previous+1
      result[#result+1]={row[1],f.payload}
      if #result>=tonumber(ARGV[9]) then break end
    elseif not f.marker or f.marker~=m.incarnation then return {'lost'} end
  end
  if #result<tonumber(ARGV[9]) and previous~=ordinal then return {'lost'} end
  return snapshot(result)
end
return snapshot({},op=='append' and m.tail or '')
"""


def _bound(value,minimum,maximum):
    if type(value) is not int or not minimum<=value<=maximum:
        raise ValueError('Invalid bounded conversation log policy')
    return value


class RedisConversationLog:
    def __init__(self,client,*,namespace,retention_ms=3_600_000,max_bytes=8*1024*1024,ttl_seconds=7200):
        self.client=client
        self.namespace=_identifier(namespace)
        self.retention_ms=_bound(retention_ms,1,604_800_000)
        self.max_bytes=_bound(max_bytes,1,256*1024*1024)
        self.ttl_seconds=_bound(ttl_seconds,1,1_209_600)
        if ttl_seconds*1000<retention_ms:
            raise ValueError('Log TTL cannot be shorter than retention')
        self._no_retry()
        # AsyncScript uses EVALSHA and reloads only after an explicit NOSCRIPT,
        # which proves that invocation did not execute. Transport failures and
        # cancellation still propagate without replaying an ambiguous append.
        self._script=self.client.register_script(_SCRIPT)

    def _no_retry(self):
        retry=self.client.get_retry()
        options=self.client.connection_pool.connection_kwargs
        if retry is None or retry.get_retries()!=0 or options.get('retry_on_timeout',False) or options.get('retry_on_error'):
            raise ValueError('Conversation log requires explicit zero automatic retry')

    def keys(self,conversation_id,generation):
        RealtimeCursor(conversation_id=conversation_id,generation=generation,stream_id='0-0')
        digest=hashlib.sha256(conversation_id.encode()).hexdigest()
        prefix=f'workmate:{self.namespace}:conversation:{{{self.namespace}-{digest}}}:{generation}'
        return prefix+':meta',prefix+':events'

    def _cursor(self,ref,position):
        return RealtimeCursor(conversation_id=ref.conversation_id,generation=ref.generation,stream_id=_text(position))

    async def _call(self,op,ref,*args):
        while True:
            self._no_retry()
            response=await self._script(keys=self.keys(ref.conversation_id,ref.generation),args=(op,
                ref.conversation_id,ref.generation,ref.incarnation,self.retention_ms,self.max_bytes,self.ttl_seconds*1000,*args),
                client=self.client)
            status=_text(response[0])
            if status!='maintenance_pending':
                break
            # This is an explicit continuation, never a network-error retry.
            # No page/window or append was produced by the incomplete slice.
            # Each slice recalculates Redis time; the final atomic invocation
            # proves the returned page meets retention at its own observation.
            await asyncio.sleep(0)
        if status=='lost': raise LogLost('Confirmed conversation log is unavailable or inconsistent')
        if status=='gap': raise ReplayGap('Cursor has no contiguous retained coverage')
        if status=='future': raise FutureCursor('Cursor is ahead of the conversation log')
        if status=='oversized': raise ValueError('Event exceeds log budget or log sequence capacity')
        if status!='ok': raise LogLost('Unknown conversation log response')
        return response

    def _window(self,ref,response):
        try:
            floor=self._cursor(ref,response[2])
            last_ordinal=int(response[7])
            # The sentinel is physical log existence proof, not a browser event.
            last=self._cursor(ref,response[4] if last_ordinal else '0-0')
            if tuple(map(int,floor.stream_id.split('-')))>tuple(map(int,last.stream_id.split('-'))):
                raise ValueError('Invalid floor ordering')
            return LogWindow(floor,self._cursor(ref,response[3]) if response[3] else None,
                last,int(response[5]),int(response[6]),last_ordinal)
        except (ValueError,TypeError,IndexError) as error:
            raise LogLost('Stored log window is invalid') from error

    async def initialize(self,conversation_id,generation):
        proposed=LogRef(conversation_id,generation,str(uuid4()))
        response=await self._call('initialize',proposed)
        try: return LogRef(conversation_id,generation,_text(response[1]))
        except ValueError as error: raise LogLost('Stored log incarnation is invalid') from error

    async def append(self,ref:LogRef,envelope:RealtimeEnvelope):
        envelope=RealtimeEnvelope.model_validate(envelope.model_dump())
        if envelope.conversation_id!=ref.conversation_id:
            raise ValueError('Event belongs to another Conversation')
        body=envelope.model_dump_json()
        if len(body.encode())>self.max_bytes: raise ValueError('Event exceeds conversation log byte budget')
        response=await self._call('append',ref,body)
        return AppendResult(self._cursor(ref,response[9]),self._window(ref,response))

    async def window(self,ref:LogRef):
        return self._window(ref,await self._call('window',ref))

    async def read(self,ref:LogRef,after:RealtimeCursor,*,count=100):
        _bound(count,1,1000)
        after=RealtimeCursor.model_validate(after.model_dump())
        if after.conversation_id!=ref.conversation_id or after.generation!=ref.generation:
            raise ValueError('Cursor belongs to another conversation log')
        response=await self._call('read',ref,after.stream_id,count)
        entries=[]
        try:
            for position,raw in response[8]:
                envelope=RealtimeEnvelope.model_validate_json(raw)
                if envelope.conversation_id!=ref.conversation_id: raise ValueError('Foreign conversation payload')
                entries.append(LogEntry(self._cursor(ref,position),envelope))
        except ValueError as error: raise LogLost('Stored event does not match the conversation protocol') from error
        return LogPage(tuple(entries),self._window(ref,response))

    async def tail(self,ref:LogRef,after:RealtimeCursor,*,count=100,block_ms=1000):
        _bound(block_ms,1,60000)
        page=await self.read(ref,after,count=count)
        if page.entries: return page
        # Raw blocking results are only a wakeup, never exposed. A second atomic
        # page proves no trim/incarnation change occurred while waiting.
        await self.client.xread({self.keys(ref.conversation_id,ref.generation)[1]:after.stream_id},
                               count=count,block=block_ms)
        return await self.read(ref,after,count=count)
