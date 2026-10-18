from __future__ import annotations

from dataclasses import dataclass, field

MEMORY_TYPES={"user_profile","collaboration_rule","ai_failure","project","decision","project_state","preference","knowledge","rejected_route"}
MEMORY_STATUSES={"已实现","进行中","已决定未开始","仅方向/想法","暂停","否决","被替代","待确认"}


@dataclass
class Memory:
    memory_id: str
    type: str
    title: str
    content: str
    status: str
    project: str
    first_seen: str
    last_confirmed: str
    evidence_message_ids: list[str]
    source_files: list[str]=field(default_factory=list)
    curated_source_files: list[str]=field(default_factory=list)
    source_priority: list[str]=field(default_factory=list)
    provenance_notes: list[str]=field(default_factory=list)
    supersedes: list[str]=field(default_factory=list)
    superseded_by: list[str]=field(default_factory=list)
    confidence: float=0.0
    created_at: str=""
    updated_at: str=""

    @classmethod
    def from_dict(cls,data:dict)->"Memory": return cls(**data)

    def validate(self,known_messages:set[str])->None:
        if self.type not in MEMORY_TYPES: raise ValueError(f"{self.memory_id}: invalid type {self.type}")
        if self.status not in MEMORY_STATUSES: raise ValueError(f"{self.memory_id}: invalid status {self.status}")
        if not self.evidence_message_ids: raise ValueError(f"{self.memory_id}: evidence is required")
        missing=set(self.evidence_message_ids)-known_messages
        if missing: raise ValueError(f"{self.memory_id}: unknown evidence {sorted(missing)}")
        if not 0 <= self.confidence <= 1: raise ValueError(f"{self.memory_id}: invalid confidence")
