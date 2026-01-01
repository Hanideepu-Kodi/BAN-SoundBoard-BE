from pydantic import BaseModel, Field


class PlaylistCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    privacy: str = "link_only"


class PlaylistUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    privacy: str | None = None


class PlaylistOut(BaseModel):
    id: str
    name: str
    description: str | None
    privacy: str
    created_at: str
    sound_count: int = 0
    share_token: str | None = None


class SoundOut(BaseModel):
    id: str
    name: str
    url: str
    tags: list[str]
    privacy: str
    duration_seconds: int | None
    created_at: str


class SoundUpdate(BaseModel):
    name: str | None = None
    tags: str | None = None
    privacy: str | None = None


class PlaylistDetail(BaseModel):
    playlist: PlaylistOut
    sounds: list[SoundOut]


class AddSoundRequest(BaseModel):
    sound_id: str = Field(..., alias="soundId")


class ReorderRequest(BaseModel):
    sound_ids: list[str] = Field(..., alias="soundIds")
