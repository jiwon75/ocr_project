from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Team Project"
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # OCR
    TESSERACT_CMD: str | None = None  # PATH 에 없으면 tesseract.exe 전체 경로 지정
    OCR_MAX_FILE_MB: int = 20
    OCR_MAX_PAGES: int = 50
    OCR_PDF_DPI: int = 300

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
