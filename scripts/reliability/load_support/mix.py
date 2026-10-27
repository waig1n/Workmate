"""Frozen 40/25/15/10/10 task mix; Skill/MCP split 10/5 percent."""
import re

CASES=('text','file','skill','text','rag','file','text','approval','file','text',
       'mcp','text','file','rag','text','skill','file','text','approval','text')


def case_for(index):return CASES[index%len(CASES)]


def fixture_kind(messages,fallback):
    for message in reversed(messages):
        for block in message.content if isinstance(message.content,list) else []:
            match=re.fullmatch(r'fixture-case:(text|file|skill|mcp|rag|approval)(?: [0-9a-f]{32})?',getattr(block,'text',''))
            if match:return match[1]
    return fallback
