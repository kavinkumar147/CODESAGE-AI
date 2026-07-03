from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    groq_api_key: str
    groq_model: str = "llama-3.3-70b-versatile"

    github_app_id: str
    github_private_key: str
    github_webhook_secret: str

    max_files_per_review: int = 40
    max_diff_chars_per_file: int = 6000
    linter_timeout_seconds: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

settings = Settings()