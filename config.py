"""
Configuration module for SIRE Voice Agent.
Loads environment variables and provides typed config access.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv(override=True)


@dataclass(frozen=True)
class VoiceLiveConfig:
    """Azure VoiceLive API configuration."""
    endpoint: str
    api_key: str | None
    model: str
    voice: str
    use_token_credential: bool

    @classmethod
    def from_env(cls) -> "VoiceLiveConfig":
        api_key = os.getenv("AZURE_VOICELIVE_API_KEY")
        use_token = os.getenv("AZURE_VOICELIVE_USE_TOKEN", "false").lower() == "true"
        return cls(
            endpoint=os.environ["AZURE_VOICELIVE_ENDPOINT"],
            api_key=api_key,
            model=os.getenv("AZURE_VOICELIVE_MODEL", "gpt-realtime"),
            voice=os.getenv("AZURE_VOICELIVE_VOICE", "en-US-Ava:DragonHDLatestNeural"),
            use_token_credential=use_token or not api_key,
        )


@dataclass(frozen=True)
class SearchConfig:
    """Azure AI Search configuration."""
    endpoint: str
    api_key: str
    group_index: str
    user_index: str
    api_version: str

    @classmethod
    def from_env(cls) -> "SearchConfig":
        return cls(
            endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
            api_key=os.environ["AZURE_SEARCH_API_KEY"],
            group_index=os.getenv("AZURE_SEARCH_GROUP_INDEX", "group-slot-mapping-index"),
            user_index=os.getenv("AZURE_SEARCH_USER_INDEX", "user-slot-mapping-index"),
            api_version=os.getenv("AZURE_SEARCH_API_VERSION", "2024-07-01"),
        )


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""
    voicelive: VoiceLiveConfig
    search: SearchConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            voicelive=VoiceLiveConfig.from_env(),
            search=SearchConfig.from_env(),
        )
