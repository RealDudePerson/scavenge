# OpenCode Instructions for Scavenge

This is a team-based scavenger hunt site built with FastAPI, SQLite, and Jinja2.

## Project Structure
- `/app/`: Core application logic, database models, and configuration.
- `/hunts/`: YAML hunt definitions (e.g., `campus_hunt.yaml`).
- `/teams/`: YAML team definitions (`teams.yaml`).
- `/templates/`: Jinja2 UI templates.
- `/uploads/`: Stored photo submissions.
- `admin_config.yaml`: Contains `admin_password`. **Do not commit this.**

## Commands
- **Run Server**: `uvicorn app.main:app --reload`
- **Dependencies**: `fastapi`, `uvicorn`, `sqlalchemy`, `aiosqlite`, `pyyaml`, `pillow`, `python-multipart`, `jinja2`.

## Operational Gotchas
- **Admin Authentication**: Required for `/admin/*` routes.
    - Login via `/admin/login` using the password from `admin_config.yaml`.
    - Protected by `is_admin` dependency and `SessionMiddleware`.
- **Database Reset**: `/admin/reset` drops all tables, recreates them, and wipes the `uploads/` directory.
- **Data Loading**: Call `/admin/load-hunts` and `/admin/load-teams` to populate the SQLite database after resets or file changes.
- **Photo Submission**: Requires EXIF GPS data. Verify camera location permissions.
- **Git**: Files in `uploads/` and `admin_config.yaml` are explicitly ignored via `.gitignore`.
